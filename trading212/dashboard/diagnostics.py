"""Work out WHY the venue is unreachable, in terms a person can act on.

Responsibility: turn a failed account poll into a cause. "Cannot reach the
broker" is true but useless: the fix for a hijacked DNS answer, a dead
network, a rejected key and a rate limit are four different actions, and the
error string alone does not distinguish them.

The decisive test is comparing what THIS machine's resolver returns for the
venue's hostname against what a public resolver returns. A VPN or a captive
network that answers with an unrelated address is the failure this account
has hit twice, and it is invisible from the error text: the connection
simply times out against an address that was never the venue's.

Everything here is read-only and best-effort. A diagnosis that cannot be
made returns "unknown" rather than a guess, because a confident wrong cause
sends the reader off fixing the wrong thing.

Out of scope: talking to the venue's API, which belongs to
trading212/client.py; deciding when to diagnose, which belongs to
trading212/dashboard/collector.py; the wording shown to the reader, which
belongs to trading212/dashboard/assets/labels.json.

Public functions:
    diagnose(host, error_text, env)   Classify one connection failure.

Constants:
    PUBLIC_DOH        tuple  DNS-over-HTTPS endpoints used as the second
                             opinion. Two independent operators, so one
                             being blocked does not by itself look like a
                             hijack.
    PROBE_TIMEOUT_SEC float  4.0. A diagnosis runs while the reader waits,
                             so every probe is short; failing to diagnose is
                             better than stalling the interface.
    CAUSES            tuple  The labels this can return, which the interface
                             renders: wrong_environment, dns_mismatch,
                             dns_fails, network_down, auth, rate_limited,
                             blocked, unknown.

Inputs:
    The OS resolver, and https://1.1.1.1 / https://dns.google as second
    opinions.
Outputs:
    None.

Change log:
    2026-08-29  Created, after a second occurrence of the machine's resolver
                answering with an unrelated address for the venue.
"""

from __future__ import annotations

__all__ = ["diagnose", "PUBLIC_DOH", "PROBE_TIMEOUT_SEC", "CAUSES"]

import socket
from typing import Any

from common.logging_setup import get_logger

log = get_logger("t212.dashboard")

PUBLIC_DOH = ("https://1.1.1.1/dns-query", "https://dns.google/resolve")
PROBE_TIMEOUT_SEC = 4.0
CAUSES = ("wrong_environment", "dns_mismatch", "dns_fails", "network_down",
          "auth", "rate_limited", "blocked", "unknown")


def _os_addresses(host: str) -> list[str]:
    """What this machine's resolver returns; the addresses actually used."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    return sorted({info[4][0] for info in infos if ":" not in info[4][0]})


def _public_addresses(host: str) -> list[str]:
    """Second opinion from public resolvers, over HTTPS.

    DNS-over-HTTPS is used rather than plain DNS because the whole point is
    to bypass whatever is answering on port 53 for this machine.
    """
    import httpx
    for endpoint in PUBLIC_DOH:
        try:
            response = httpx.get(endpoint, params={"name": host, "type": "A"},
                                 headers={"accept": "application/dns-json"},
                                 timeout=PROBE_TIMEOUT_SEC)
            if response.status_code != 200:
                continue
            answers = response.json().get("Answer") or []
            addresses = sorted({a["data"] for a in answers
                                if a.get("type") == 1 and a.get("data")})
            if addresses:
                return addresses
        except Exception:
            continue
    return []


def _connects(address: str, host: str) -> bool:
    """Whether a TLS-capable TCP connection to one address succeeds."""
    try:
        with socket.create_connection((address, 443),
                                      timeout=PROBE_TIMEOUT_SEC):
            return True
    except OSError:
        return False


def diagnose(host: str, error_text: str = "",
             env: str | None = None) -> dict[str, Any]:
    """Classify why the venue could not be reached.

    Returns a mapping with the cause label, the evidence behind it, and
    whether the venue is reachable once the resolver is bypassed -- which is
    what tells a reader that the network is fine and only the name lookup is
    wrong.
    """
    lowered = (error_text or "").lower()
    if "401" in lowered or "bad api key" in lowered:
        if env == "paper":
            # QUANT_ENV defaults to paper, which points at the demo host, and
            # a live-only key is rejected there with exactly this 401. The
            # symptom is indistinguishable from a bad key unless the
            # environment is taken into account, and the fix is entirely
            # different: set QUANT_ENV rather than replace the key.
            return {"cause": "wrong_environment",
                    "evidence": {"env": env, "host": host,
                                 "error": error_text[:200]}}
        return {"cause": "auth", "evidence": error_text[:200]}
    if "429" in lowered or "rate limit" in lowered:
        return {"cause": "rate_limited", "evidence": error_text[:200]}

    os_addrs = _os_addresses(host)
    public_addrs = _public_addresses(host)
    evidence: dict[str, Any] = {"host": host, "os_addresses": os_addrs,
                                "public_addresses": public_addrs}

    if not public_addrs:
        # No second opinion available. Either the whole network is down or
        # the public resolvers are blocked too; either way the local answer
        # cannot be judged.
        cause = "network_down" if not os_addrs or not any(
            _connects(a, host) for a in os_addrs) else "unknown"
        return {"cause": cause, "evidence": evidence}

    public_ok = any(_connects(a, host) for a in public_addrs)
    if not os_addrs:
        return {"cause": "dns_fails", "evidence": evidence,
                "reachable_bypassing_dns": public_ok}

    if set(os_addrs) != set(public_addrs):
        os_ok = any(_connects(a, host) for a in os_addrs)
        if public_ok and not os_ok:
            # The machine is being sent somewhere that is not the venue,
            # while the real venue answers normally.
            return {"cause": "dns_mismatch", "evidence": evidence,
                    "reachable_bypassing_dns": True}
        return {"cause": "unknown", "evidence": evidence,
                "reachable_bypassing_dns": public_ok}

    # Same answers everywhere, so the name is right and the traffic is not
    # getting through.
    return {"cause": "blocked" if public_ok else "network_down",
            "evidence": evidence, "reachable_bypassing_dns": public_ok}
