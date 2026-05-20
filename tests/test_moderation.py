"""Tests for the moderation layer: censors, bans, durations, file reloads."""

import time

import pytest

from master_server.moderation import (
    BanList,
    Censor,
    _parse_asn,
    _parse_duration,
    _parse_until,
    parse_ban_target,
)


# --------------------------------------------------------------------------- #
# Censor
# --------------------------------------------------------------------------- #


def test_censor_matches_case_insensitively(tmp_path):
    p = tmp_path / "censors.txt"
    p.write_text("casino\nfree gems\n# comment line\n\n")
    c = Censor(str(p))
    assert c.is_censored("Best CASINO ever")
    assert c.is_censored("come play", "Free Gems!! click here")
    assert not c.is_censored("Polite Court", "A friendly server")


def test_censor_disabled_when_path_missing(tmp_path):
    c = Censor(str(tmp_path / "does-not-exist.txt"))
    assert not c.is_censored("anything", "at all")


def test_censor_reloads_on_mtime_change(tmp_path):
    p = tmp_path / "censors.txt"
    p.write_text("foo\n")
    c = Censor(str(p))
    assert c.is_censored("foo bar")
    # Ensure the new write has a strictly later mtime even on coarse clocks.
    p.write_text("bar\n")
    import os

    os.utime(p, (time.time() + 2, time.time() + 2))
    assert c.is_censored("bar baz")
    assert not c.is_censored("foo only")


# --------------------------------------------------------------------------- #
# BanList -- file
# --------------------------------------------------------------------------- #


def test_banlist_file_permanent_and_cidr(tmp_path):
    p = tmp_path / "bans.txt"
    p.write_text("1.2.3.4\n10.0.0.0/8\n# trailing comment\n")
    b = BanList(str(p))
    assert b.is_banned("1.2.3.4")
    assert b.is_banned("10.5.5.5")
    assert not b.is_banned("8.8.8.8")


def test_banlist_file_until(tmp_path):
    p = tmp_path / "bans.txt"
    # Until far in the past -> already expired.
    p.write_text("5.5.5.5 until=2000-01-01T00:00:00Z\n")
    assert not BanList(str(p)).is_banned("5.5.5.5")


def test_banlist_file_for_duration(tmp_path):
    p = tmp_path / "bans.txt"
    p.write_text("6.6.6.6 for=24h reason=spam\n7.7.7.7 for=1s\n")
    b = BanList(str(p))
    assert b.is_banned("6.6.6.6")
    # Simulate time travel by overriding ``now``.
    far_future = time.time() + 3600 * 25
    assert not b.is_banned("6.6.6.6", now=far_future)


# --------------------------------------------------------------------------- #
# BanList -- admin (in-memory)
# --------------------------------------------------------------------------- #


def test_banlist_admin_permanent():
    b = BanList(None)
    b.add("1.2.3.4")
    assert b.is_banned("1.2.3.4")


def test_banlist_admin_temporary_expires():
    b = BanList(None)
    b.add("2.2.2.2", duration_minutes=10, now=1000.0)
    assert b.is_banned("2.2.2.2", now=1000.0)
    assert b.is_banned("2.2.2.2", now=1000.0 + 599)
    assert not b.is_banned("2.2.2.2", now=1000.0 + 601)


def test_banlist_admin_per_ip_durations_are_independent():
    # The user explicitly asked: durations are per-IP. A short ban on one
    # address must not affect a longer ban on another.
    b = BanList(None)
    b.add("1.1.1.1", duration_minutes=5, now=0.0)
    b.add("2.2.2.2", duration_minutes=60, now=0.0)
    b.add("3.3.3.3", now=0.0)  # permanent
    t = 10 * 60
    assert not b.is_banned("1.1.1.1", now=t)
    assert b.is_banned("2.2.2.2", now=t)
    assert b.is_banned("3.3.3.3", now=t)
    # Hours later only the permanent one survives.
    t = 24 * 3600
    assert not b.is_banned("2.2.2.2", now=t)
    assert b.is_banned("3.3.3.3", now=t)


def test_banlist_remove_admin_entry():
    b = BanList(None)
    b.add("9.9.9.9")
    assert b.is_banned("9.9.9.9")
    assert b.remove("9.9.9.9")
    assert not b.is_banned("9.9.9.9")
    # Removing a non-existent ban is a no-op.
    assert not b.remove("9.9.9.9")


def test_banlist_add_invalid_ip_raises():
    b = BanList(None)
    with pytest.raises(ValueError):
        b.add("not-an-ip")


def test_banlist_add_invalid_duration_raises():
    b = BanList(None)
    with pytest.raises(ValueError):
        b.add("1.2.3.4", duration_minutes=0)
    with pytest.raises(ValueError):
        b.add("1.2.3.4", duration_minutes=-5)


def test_banlist_list_active_includes_source_and_reason(tmp_path):
    p = tmp_path / "bans.txt"
    p.write_text("1.2.3.4 reason=fileban\n")
    b = BanList(str(p))
    b.add("5.5.5.5", reason="adminban")
    listed = {e["network"]: e for e in b.list_active()}
    assert listed["1.2.3.4/32"]["source"] == "file"
    assert listed["1.2.3.4/32"]["reason"] == "fileban"
    assert listed["5.5.5.5/32"]["source"] == "admin"
    assert listed["5.5.5.5/32"]["reason"] == "adminban"


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30s", 30),
        ("15m", 15 * 60),
        ("24h", 24 * 3600),
        ("7d", 7 * 86400),
        ("42", 42 * 60),  # bare number = minutes
    ],
)
def test_parse_duration_units(raw, expected):
    assert _parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "10x", "-5m", "0h"])
def test_parse_duration_rejects_garbage(raw):
    assert _parse_duration(raw) is None


def test_parse_until_handles_z_suffix():
    assert _parse_until("2030-01-01T00:00:00Z") is not None


# --------------------------------------------------------------------------- #
# ASN parsing & bans
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AS15169", 15169),
        ("as15169", 15169),
        ("ASN15169", 15169),
        ("asn15169", 15169),
        ("AS0", 0),
    ],
)
def test_parse_asn_accepts_common_forms(raw, expected):
    assert _parse_asn(raw) == expected


@pytest.mark.parametrize("raw", ["", "15169", "ASfoo", "A15169", "AS", "1.2.3.4"])
def test_parse_asn_rejects_garbage(raw):
    assert _parse_asn(raw) is None


def test_parse_ban_target_prefers_asn_over_ip():
    assert parse_ban_target("AS15169") == ("asn", 15169)
    kind, value = parse_ban_target("10.0.0.0/8")
    assert kind == "net"
    assert str(value) == "10.0.0.0/8"
    assert parse_ban_target("not-a-thing") is None


def test_asn_ban_uses_lookup():
    # The lookup function is the only knob -- no real .mmdb file in the test.
    lookups = {"1.2.3.4": 15169, "5.5.5.5": 13335}
    b = BanList(None, asn_lookup=lambda ip: lookups.get(ip))
    b.add("AS15169")
    assert b.is_banned("1.2.3.4")     # Google ASN ban catches Google IP
    assert not b.is_banned("5.5.5.5")  # Cloudflare IP unaffected
    assert not b.is_banned("9.9.9.9")  # unknown IP -> no asn -> no match


def test_asn_ban_inert_without_lookup():
    # Without an asn_lookup the ban is stored but never matches -- the
    # operator gets a heads-up at startup but heartbeats keep flowing.
    b = BanList(None, asn_lookup=None)
    b.add("AS15169")
    assert not b.is_banned("1.2.3.4")
    # The entry is still listed so operators can see it's configured.
    listed = b.list_active()
    assert listed[0]["target"] == "AS15169"
    assert listed[0]["kind"] == "asn"


def test_asn_ban_with_duration_expires():
    b = BanList(None, asn_lookup=lambda ip: 15169)
    b.add("AS15169", duration_minutes=10, now=1000.0)
    assert b.is_banned("1.2.3.4", now=1000.0)
    assert not b.is_banned("1.2.3.4", now=1000.0 + 601)


def test_asn_ban_unban_round_trip():
    b = BanList(None, asn_lookup=lambda ip: 15169)
    b.add("AS15169")
    assert b.is_banned("1.2.3.4")
    assert b.remove("AS15169")
    assert not b.is_banned("1.2.3.4")
    # Removing a non-existent ASN ban is a no-op.
    assert not b.remove("AS15169")


def test_asn_lookup_failure_does_not_crash(caplog):
    def boom(ip):
        raise RuntimeError("db corrupted")

    b = BanList(None, asn_lookup=boom)
    b.add("AS15169")
    # Should swallow the exception, log it, and treat as "not banned".
    assert not b.is_banned("1.2.3.4")


def test_bans_file_parses_asn_entries(tmp_path):
    p = tmp_path / "bans.txt"
    p.write_text("AS15169\nAS13335 for=24h reason=cloudflare\n")
    b = BanList(str(p), asn_lookup=lambda ip: 15169)
    assert b.is_banned("8.8.8.8")  # Google
    listed = {e["target"]: e for e in b.list_active()}
    assert "AS15169" in listed
    assert listed["AS13335"]["reason"] == "cloudflare"


def test_asn_lookup_not_called_when_no_asn_bans():
    # Optimisation: do not pay for the lookup on the common path.
    calls = []

    def lookup(ip):
        calls.append(ip)
        return 12345

    b = BanList(None, asn_lookup=lookup)
    b.add("1.2.3.4")  # only IP bans
    b.is_banned("9.9.9.9")
    assert calls == []
