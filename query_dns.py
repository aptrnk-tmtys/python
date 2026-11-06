#!/usr/bin/env python3
"""Query the same DNS name against several nameservers (Windows/Mac/Linux).

Nameservers are read from ~/.query_dns.txt (created automatically if missing).
Requires: pip install dnspython
"""
import argparse
import ipaddress
import platform
import re
import socket
import sys
from pathlib import Path

# --- Dependency check: give a friendly message instead of a traceback ---
try:
    import dns.exception
    import dns.resolver
    import dns.reversename
except ImportError:
    sys.exit("Missing dependency. Install it with:  pip install dnspython")

CONFIG_PATH = Path.home() / ".query_dns.txt"  # works the same on every OS
TIMEOUT = 3  # seconds to wait per query

DEFAULT_CONFIG = """\
# This script contains variables for the query_dns.py script
DefaultDomain=example.lan  # in case --domain isn't used
DESCONE="First nameserver"  # A custom description for first nameserver
NSONE=ns1.example.lan. # first nameserver to check (can also be an IP)
DESCTWO="Second nameserver"  # A custom description for second nameserver
NSTWO=ns2.example.lan. # second nameserver to check (can also be an IP)
DESCTHREE="Third nameserver"  # A custom description for third nameserver
NSTHREE=ns3.example.lan. # third nameserver to check (can also be an IP)
DESCFOUR="Fourth nameserver"  # A custom description for fourth nameserver
NSFOUR=ns4.example.lan. # fourth nameserver to check (can also be an IP)
"""

# A DNS label: letters/digits/hyphens, 1-63 chars, no leading/trailing hyphen
LABEL = re.compile(r"^(?!-)[A-Za-z0-9_-]{1,63}(?<!-)$")


def is_valid_name(name):
    """Sanity check a hostname/domain: total length and each label."""
    name = name.rstrip(".")
    return 0 < len(name) <= 253 and all(LABEL.match(p) for p in name.split("."))


def ensure_config():
    """Create the config file only if it doesn't exist (safe to re-run)."""
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
    print(f"Created {CONFIG_PATH} - edit it with your own nameservers.")


def load_config():
    """Parse KEY=VALUE lines, ignoring blank lines and # comments."""
    values = {}
    for number, line in enumerate(CONFIG_PATH.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"Warning: ignoring line {number} in {CONFIG_PATH}: {line}")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value.startswith('"'):  # quoted value: keep text up to closing quote
            value = value[1:].split('"', 1)[0]
        else:  # unquoted value: drop the trailing "# comment"
            value = value.split("#", 1)[0].strip()
        values[key] = value
    return values


def resolve_ns_ip(nameserver):
    """Return an IP for a nameserver (dnspython needs an IP, not a name)."""
    try:
        ipaddress.ip_address(nameserver)
        return nameserver  # already an IP
    except ValueError:
        return socket.getaddrinfo(nameserver, 53, proto=socket.IPPROTO_UDP)[0][4][0]


def lookup(resolver, name, rtypes):
    """Ask one nameserver for each record type; return unique answer lines."""
    lines = []
    for rtype in rtypes:
        try:
            answer = resolver.resolve(name, rtype, raise_on_no_answer=False)
        except dns.resolver.NXDOMAIN:
            return ["NXDOMAIN (name does not exist)"]
        except dns.exception.Timeout:
            return ["Timed out (no response)"]
        except dns.resolver.NoNameservers:
            return ["Server refused or failed the query"]
        except dns.exception.DNSException as err:
            return [f"DNS error: {err.__class__.__name__}"]
        # Keep only the answer section (A, AAAA, CNAME, PTR...), skipping duplicates
        for rrset in answer.response.answer:
            for line in rrset.to_text().splitlines():
                if line not in lines:
                    lines.append(line)
    return lines or ["No records found"]


def parse_args():
    """Define and validate the command-line flags."""
    p = argparse.ArgumentParser(description="Query one DNS name against all nameservers in ~/.query_dns.txt")
    p.add_argument("-s", "--subdomain", metavar="SUBDOMAIN", help="subdomain to look up (uses --domain or DefaultDomain)")
    p.add_argument("-d", "--domain", metavar="EXAMPLE.COM", help="domain to append to --subdomain")
    p.add_argument("-f", "--fqdn", metavar="SUBDOMAIN.EXAMPLE.COM", help="full name to look up")
    p.add_argument("-r", "--reverse", metavar="IPADDRESS", help="IP address to check for a reverse (PTR) entry")
    args = p.parse_args()

    # Need at least one thing to look up
    if not (args.subdomain or args.fqdn or args.reverse):
        p.print_help()
        sys.exit(1)
    # Reject confusing flag combinations
    if args.fqdn and (args.subdomain or args.domain):
        p.error("--fqdn cannot be combined with --subdomain/--domain")
    if args.domain and not args.subdomain:
        p.error("--domain needs --subdomain (use --fqdn for a full name)")
    # Validate values early so mistakes are caught before any queries
    if args.reverse:
        try:
            ipaddress.ip_address(args.reverse)
        except ValueError:
            p.error(f"invalid IP address: {args.reverse}")
    for value in (args.subdomain, args.domain, args.fqdn):
        if value and not is_valid_name(value):
            p.error(f"invalid DNS name: {value}")
    return args


def main():
    args = parse_args()

    # Step 1: report the operating system
    print(f"OS: {platform.system()} {platform.release()}")

    # Step 2: make sure the config exists, then read it
    ensure_config()
    config = load_config()
    print(f"Config: {CONFIG_PATH}")

    # Step 3: work out the full name to look up
    fqdn = args.fqdn
    if args.subdomain:
        domain = args.domain or config.get("DefaultDomain", "")
        if not is_valid_name(domain):
            sys.exit("No valid domain: use --domain or set DefaultDomain in the config.")
        fqdn = f"{args.subdomain}.{domain}"

    # Step 4: collect (description, nameserver) pairs, skipping empty ones
    servers = []
    for desc_key, ns_key in (("DESCONE", "NSONE"), ("DESCTWO", "NSTWO"),
                             ("DESCTHREE", "NSTHREE"), ("DESCFOUR", "NSFOUR")):
        ns = config.get(ns_key, "").rstrip(".")
        if ns:
            servers.append((config.get(desc_key, ""), ns))
    if not servers:
        sys.exit(f"No nameservers defined in {CONFIG_PATH}")

    # Step 5: query each nameserver and print its answers
    for desc, ns in servers:
        print()
        #print(f"{desc or 'Nameserver'} ({ns})")
        print(f"---------- {ns} ({desc or 'Nameserver'})")
        try:
            resolver = dns.resolver.Resolver(configure=False)  # ignore OS DNS settings
            resolver.nameservers = [resolve_ns_ip(ns)]
            resolver.lifetime = TIMEOUT
        except (socket.gaierror, OSError):
            print("Cannot resolve this nameserver's address")
            continue

        if fqdn:  # forward lookup
            print("\n".join(lookup(resolver, fqdn, ("A", "AAAA", "CNAME"))))
        if args.reverse:  # reverse lookup (PTR)
            ptr_name = dns.reversename.from_address(args.reverse)
            print("\n".join(lookup(resolver, ptr_name, ("PTR",))))


if __name__ == "__main__":
    main()
