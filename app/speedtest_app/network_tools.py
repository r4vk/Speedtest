"""
Network diagnostic tools — on-demand execution, no persistence.

Each tool is an async function accepting a params dict and returning a result dict.
The unified run_tool() dispatcher wraps execution with timing, error handling,
and a concurrency semaphore.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import dns.message
import dns.name
import dns.rdatatype
import dns.resolver
import dns.reversename
import httpx

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_HOSTNAME_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$"
)
_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _validate_hostname(value: str) -> str:
    v = value.strip()
    if not v or len(v) > 253:
        raise ValueError("Nieprawidlowa nazwa hosta")
    if not _HOSTNAME_RE.match(v) and not _IP_RE.match(v):
        raise ValueError(f"Nieprawidlowy host lub IP: {v}")
    return v


def _validate_ip(value: str) -> str:
    v = value.strip()
    try:
        ipaddress.ip_address(v)
    except ValueError:
        raise ValueError(f"Nieprawidlowy adres IP: {v}")
    return v


def _validate_port_spec(spec: str, max_ports: int = 1000) -> list[int]:
    ports: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
            if a < 1 or b > 65535 or a > b:
                raise ValueError(f"Nieprawidlowy zakres portow: {part}")
            if (b - a + 1) > max_ports:
                raise ValueError(f"Zakres zbyt duzy: max {max_ports}")
            ports.update(range(a, b + 1))
        else:
            p = int(part)
            if p < 1 or p > 65535:
                raise ValueError(f"Nieprawidlowy port: {p}")
            ports.add(p)
    if len(ports) > max_ports:
        raise ValueError(f"Za duzo portow: {len(ports)} > {max_ports}")
    if not ports:
        raise ValueError("Nie podano portow")
    return sorted(ports)


def _validate_subnet(subnet: str, min_prefix: int = 24) -> str:
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        raise ValueError(f"Nieprawidlowa podsiec: {subnet}")
    if net.prefixlen < min_prefix:
        raise ValueError(f"Podsiec zbyt duza: /{net.prefixlen} < /{min_prefix}")
    return str(net)


def _positive_int(value: Any, name: str, lo: int = 1, hi: int = 10000) -> int:
    v = int(value)
    if v < lo or v > hi:
        raise ValueError(f"{name} musi byc w zakresie {lo}-{hi}")
    return v


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

async def _run_subprocess(
    cmd: list[str],
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"Timeout ({timeout}s): {' '.join(cmd[:3])}")


# ---------------------------------------------------------------------------
# DNS tools
# ---------------------------------------------------------------------------

PUBLIC_DNS_SERVERS = [
    ("Cloudflare", "1.1.1.1"),
    ("Google", "8.8.8.8"),
    ("Quad9", "9.9.9.9"),
    ("OpenDNS", "208.67.222.222"),
    ("Cloudflare 2", "1.0.0.1"),
    ("Google 2", "8.8.4.4"),
    ("Comodo", "8.26.56.26"),
    ("Neustar", "64.6.64.6"),
]


async def tool_dns_resolve_time(params: dict) -> dict:
    domain = _validate_hostname(params.get("domain", "google.com"))
    dns_server = params.get("dns_server", "8.8.8.8").strip()
    record_type = params.get("record_type", "A").upper()

    if dns_server:
        _validate_ip(dns_server)

    rdtype = dns.rdatatype.from_text(record_type)
    resolver = dns.resolver.Resolver()
    if dns_server:
        resolver.nameservers = [dns_server]
    resolver.lifetime = 8

    def _resolve():
        t0 = time.perf_counter()
        answers = resolver.resolve(domain, rdtype)
        elapsed = (time.perf_counter() - t0) * 1000
        return elapsed, [rdata.to_text() for rdata in answers]

    elapsed_ms, records = await asyncio.to_thread(_resolve)
    return {
        "domain": domain,
        "dns_server": dns_server or "systemowy",
        "record_type": record_type,
        "resolve_time_ms": round(elapsed_ms, 2),
        "answers": records,
    }


async def tool_dns_propagation(params: dict) -> dict:
    domain = _validate_hostname(params.get("domain", "example.com"))
    record_type = params.get("record_type", "A").upper()
    rdtype = dns.rdatatype.from_text(record_type)

    async def _query_server(name: str, ip: str):
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [ip]
        resolver.lifetime = 8

        def _do():
            t0 = time.perf_counter()
            try:
                answers = resolver.resolve(domain, rdtype)
                elapsed = (time.perf_counter() - t0) * 1000
                return elapsed, [r.to_text() for r in answers], None
            except Exception as e:
                elapsed = (time.perf_counter() - t0) * 1000
                return elapsed, [], str(e)

        elapsed, records, err = await asyncio.to_thread(_do)
        return {
            "name": name,
            "ip": ip,
            "answers": records,
            "time_ms": round(elapsed, 2),
            "error": err,
        }

    tasks = [_query_server(n, ip) for n, ip in PUBLIC_DNS_SERVERS]
    results = await asyncio.gather(*tasks)
    servers = list(results)

    # Check consistency
    answer_sets = [frozenset(s["answers"]) for s in servers if not s["error"]]
    consistent = len(set(answer_sets)) <= 1 if answer_sets else False

    return {
        "domain": domain,
        "record_type": record_type,
        "servers": servers,
        "consistent": consistent,
    }


async def tool_dns_leak_test(params: dict) -> dict:
    detected: list[dict] = []

    # Method 1: whoami.akamai.net
    async def _akamai():
        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 8
            answers = await asyncio.to_thread(
                lambda: resolver.resolve("whoami.akamai.net", "A")
            )
            for r in answers:
                return r.to_text()
        except Exception:
            return None

    # Method 2: o-o.myaddr.l.google.com TXT
    async def _google():
        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 8
            answers = await asyncio.to_thread(
                lambda: resolver.resolve("o-o.myaddr.l.google.com", "TXT")
            )
            ips = []
            for r in answers:
                txt = r.to_text().strip('"')
                ips.append(txt)
            return ips
        except Exception:
            return []

    akamai_ip, google_ips = await asyncio.gather(_akamai(), _google())

    all_ips = set()
    if akamai_ip:
        all_ips.add(akamai_ip)
    for ip in google_ips:
        try:
            ipaddress.ip_address(ip)
            all_ips.add(ip)
        except ValueError:
            pass

    # Reverse DNS and GeoIP for detected IPs
    for ip in all_ips:
        entry: dict[str, Any] = {"ip": ip}
        try:
            hostname = await asyncio.to_thread(lambda ip=ip: socket.getfqdn(ip))
            entry["hostname"] = hostname if hostname != ip else ""
        except Exception:
            entry["hostname"] = ""
        detected.append(entry)

    return {
        "detected_dns_servers": detected,
        "count": len(detected),
        "method": "whoami.akamai.net + o-o.myaddr.l.google.com",
    }


async def tool_dns_doh_dot_status(params: dict) -> dict:
    dns_server = params.get("dns_server", "1.1.1.1").strip()
    _validate_ip(dns_server)

    result: dict[str, Any] = {"dns_server": dns_server}

    # Plain DNS
    def _plain():
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [dns_server]
        resolver.lifetime = 8
        t0 = time.perf_counter()
        try:
            resolver.resolve("google.com", "A")
            return round((time.perf_counter() - t0) * 1000, 2), True, None
        except Exception as e:
            return round((time.perf_counter() - t0) * 1000, 2), False, str(e)

    plain_ms, plain_ok, plain_err = await asyncio.to_thread(_plain)
    result["plain_dns"] = {"available": plain_ok, "time_ms": plain_ms, "error": plain_err}

    # DoT (DNS over TLS, port 853)
    async def _dot():
        t0 = time.perf_counter()
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(dns_server, 853, ssl=ctx), timeout=5
            )
            # Build a simple DNS query for google.com A
            query = dns.message.make_query("google.com", "A")
            wire = query.to_wire()
            writer.write(struct.pack("!H", len(wire)) + wire)
            await writer.drain()
            length_data = await asyncio.wait_for(reader.readexactly(2), timeout=5)
            length = struct.unpack("!H", length_data)[0]
            await asyncio.wait_for(reader.readexactly(length), timeout=5)
            writer.close()
            elapsed = round((time.perf_counter() - t0) * 1000, 2)
            return {"available": True, "time_ms": elapsed, "error": None}
        except Exception as e:
            elapsed = round((time.perf_counter() - t0) * 1000, 2)
            return {"available": False, "time_ms": elapsed, "error": str(e)}

    # DoH (DNS over HTTPS)
    async def _doh():
        doh_urls = [
            f"https://{dns_server}/dns-query",
            f"https://dns.google/dns-query" if dns_server.startswith("8.8.") else None,
            f"https://cloudflare-dns.com/dns-query" if dns_server.startswith("1.") else None,
        ]
        doh_url = next((u for u in doh_urls if u), doh_urls[0])
        t0 = time.perf_counter()
        try:
            query = dns.message.make_query("google.com", "A")
            wire = query.to_wire()
            async with httpx.AsyncClient(verify=False, timeout=5) as client:
                resp = await client.post(
                    doh_url,
                    content=wire,
                    headers={
                        "Content-Type": "application/dns-message",
                        "Accept": "application/dns-message",
                    },
                )
            elapsed = round((time.perf_counter() - t0) * 1000, 2)
            ok = resp.status_code == 200
            return {"available": ok, "time_ms": elapsed, "url": doh_url, "error": None if ok else f"HTTP {resp.status_code}"}
        except Exception as e:
            elapsed = round((time.perf_counter() - t0) * 1000, 2)
            return {"available": False, "time_ms": elapsed, "url": doh_url, "error": str(e)}

    dot_result, doh_result = await asyncio.gather(_dot(), _doh())
    result["dot"] = dot_result
    result["doh"] = doh_result

    return result


async def tool_dns_server_comparison(params: dict) -> dict:
    domain = _validate_hostname(params.get("domain", "google.com"))
    iterations = _positive_int(params.get("iterations", 5), "iterations", 1, 20)

    async def _bench(name: str, ip: str):
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [ip]
        resolver.lifetime = 8
        times: list[float] = []

        def _do():
            for _ in range(iterations):
                t0 = time.perf_counter()
                try:
                    resolver.resolve(domain, "A")
                except Exception:
                    pass
                times.append((time.perf_counter() - t0) * 1000)

        await asyncio.to_thread(_do)
        if times:
            return {
                "name": name,
                "ip": ip,
                "avg_ms": round(sum(times) / len(times), 2),
                "min_ms": round(min(times), 2),
                "max_ms": round(max(times), 2),
                "iterations": len(times),
            }
        return {"name": name, "ip": ip, "avg_ms": None, "min_ms": None, "max_ms": None, "iterations": 0}

    # Also add system default
    all_servers = list(PUBLIC_DNS_SERVERS[:6])
    tasks = [_bench(n, ip) for n, ip in all_servers]
    results = await asyncio.gather(*tasks)

    return {
        "domain": domain,
        "iterations": iterations,
        "servers": sorted(results, key=lambda s: s["avg_ms"] or 99999),
    }


# ---------------------------------------------------------------------------
# Routing tools
# ---------------------------------------------------------------------------

async def tool_traceroute(params: dict) -> dict:
    target = _validate_hostname(params.get("target", "google.com"))
    max_hops = _positive_int(params.get("max_hops", 30), "max_hops", 1, 40)

    rc, stdout, stderr = await _run_subprocess(
        ["traceroute", "-n", "-m", str(max_hops), "-w", "2", target],
        timeout=60,
    )

    hops: list[dict] = []
    # Parse lines like: " 1  192.168.1.1  1.234 ms  1.123 ms  1.345 ms"
    for line in stdout.strip().splitlines()[1:]:  # Skip header
        parts = line.split()
        if not parts:
            continue
        try:
            hop_num = int(parts[0])
        except ValueError:
            continue

        ip = parts[1] if len(parts) > 1 else "*"
        rtt_values: list[float | None] = []
        for p in parts[2:]:
            if p == "ms":
                continue
            if p == "*":
                rtt_values.append(None)
            else:
                try:
                    rtt_values.append(float(p))
                except ValueError:
                    pass

        hops.append({
            "hop": hop_num,
            "ip": ip if ip != "*" else None,
            "rtt_ms": rtt_values[:3],
        })

    # Resolve target IP
    resolved_ip = None
    try:
        resolved_ip = await asyncio.to_thread(lambda: socket.gethostbyname(target))
    except Exception:
        pass

    return {
        "target": target,
        "resolved_ip": resolved_ip,
        "hops": hops,
        "raw": stdout if not hops else None,
    }


async def tool_mtr(params: dict) -> dict:
    target = _validate_hostname(params.get("target", "google.com"))
    count = _positive_int(params.get("count", 10), "count", 1, 100)

    rc, stdout, stderr = await _run_subprocess(
        ["mtr", "--report", "--json", "-c", str(count), "-n", target],
        timeout=120,
    )

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"target": target, "report": [], "raw": stdout}

    report_data = data.get("report", {})
    hubs = report_data.get("hubs", [])

    report: list[dict] = []
    for hub in hubs:
        report.append({
            "hop": hub.get("count", 0),
            "host": hub.get("host", "*"),
            "loss_pct": hub.get("Loss%", 0),
            "sent": hub.get("Snt", count),
            "avg_ms": hub.get("Avg", 0),
            "best_ms": hub.get("Best", 0),
            "worst_ms": hub.get("Wrst", 0),
            "stdev_ms": hub.get("StDev", 0),
        })

    return {
        "target": target,
        "count": count,
        "report": report,
    }


async def tool_bgp_as_path(params: dict) -> dict:
    target = params.get("target", "8.8.8.8").strip()

    # Resolve hostname to IP if needed
    ip = target
    try:
        ipaddress.ip_address(target)
    except ValueError:
        _validate_hostname(target)
        ip = await asyncio.to_thread(lambda: socket.gethostbyname(target))

    from ipwhois import IPWhois

    def _lookup():
        obj = IPWhois(ip)
        result = obj.lookup_rdap(asn_methods=["whois", "dns", "http"])
        return result

    rdap = await asyncio.to_thread(_lookup)

    return {
        "target": target,
        "ip": ip,
        "asn": f"AS{rdap.get('asn', '?')}",
        "as_name": rdap.get("asn_description", ""),
        "as_country": rdap.get("asn_country_code", ""),
        "prefix": rdap.get("asn_cidr", ""),
        "network_name": rdap.get("network", {}).get("name", ""),
    }


async def tool_reverse_dns(params: dict) -> dict:
    ip = _validate_ip(params.get("ip", "8.8.8.8"))

    def _lookup():
        rev_name = dns.reversename.from_address(ip)
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 8
        t0 = time.perf_counter()
        try:
            answers = resolver.resolve(rev_name, "PTR")
            elapsed = (time.perf_counter() - t0) * 1000
            return [r.to_text().rstrip(".") for r in answers], round(elapsed, 2), None
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return [], round(elapsed, 2), str(e)

    hostnames, elapsed_ms, err = await asyncio.to_thread(_lookup)
    return {
        "ip": ip,
        "hostnames": hostnames,
        "time_ms": elapsed_ms,
        "error": err,
    }


# ---------------------------------------------------------------------------
# Connection info tools
# ---------------------------------------------------------------------------

async def tool_mtu_discovery(params: dict) -> dict:
    target = _validate_hostname(params.get("target", "google.com"))

    async def _ping_with_size(size: int) -> bool:
        try:
            rc, _, _ = await _run_subprocess(
                ["ping", "-M", "do", "-s", str(size), "-c", "1", "-W", "2", target],
                timeout=5,
            )
            return rc == 0
        except Exception:
            return False

    # Binary search for MTU
    lo, hi = 68, 1500
    path_mtu = lo

    while lo <= hi:
        mid = (lo + hi) // 2
        if await _ping_with_size(mid):
            path_mtu = mid
            lo = mid + 1
        else:
            hi = mid - 1

    # MTU = payload + 28 bytes (20 IP header + 8 ICMP header)
    return {
        "target": target,
        "path_mtu_payload": path_mtu,
        "mtu": path_mtu + 28,
    }


async def tool_geoip(params: dict) -> dict:
    ip = params.get("ip", "").strip()

    url = f"http://ip-api.com/json/{ip}" if ip else "http://ip-api.com/json/"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") == "fail":
        raise ValueError(data.get("message", "Blad API"))

    return {
        "ip": data.get("query", ip),
        "country": data.get("country", ""),
        "country_code": data.get("countryCode", ""),
        "region": data.get("regionName", ""),
        "city": data.get("city", ""),
        "isp": data.get("isp", ""),
        "org": data.get("org", ""),
        "as": data.get("as", ""),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "timezone": data.get("timezone", ""),
    }


async def tool_public_ip(params: dict) -> dict:
    services = [
        ("ifconfig.me", "https://ifconfig.me/ip"),
        ("api.ipify.org", "https://api.ipify.org"),
        ("icanhazip.com", "https://icanhazip.com"),
    ]

    async def _query(name: str, url: str):
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "curl/8.0"})
                return name, resp.text.strip(), None
        except Exception as e:
            return name, None, str(e)

    results = await asyncio.gather(*[_query(n, u) for n, u in services])

    sources = {}
    for name, ip, err in results:
        sources[name] = ip if not err else f"blad: {err}"

    ips = [ip for _, ip, err in results if not err and ip]
    consistent = len(set(ips)) <= 1 if ips else False

    return {
        "ip": ips[0] if ips else None,
        "sources": sources,
        "consistent": consistent,
    }


async def tool_nat_type(params: dict) -> dict:
    def _detect():
        try:
            import stun
            nat_type, external_ip, external_port = stun.get_ip_info(
                stun_host="stun.l.google.com",
                stun_port=19302,
            )
            return {
                "nat_type": nat_type,
                "external_ip": external_ip,
                "external_port": external_port,
            }
        except Exception as e:
            raise ValueError(f"Blad detekcji NAT: {e}")

    return await asyncio.to_thread(_detect)


# ---------------------------------------------------------------------------
# Local network tools
# ---------------------------------------------------------------------------

async def tool_port_scan(params: dict) -> dict:
    target = _validate_hostname(params.get("target", "192.168.1.1"))
    port_spec = params.get("ports", "22,80,443,8080,8443,3389,21,25,53,3306,5432")
    timeout_s = _positive_int(params.get("timeout", 2), "timeout", 1, 10)
    ports = _validate_port_spec(port_spec)

    WELL_KNOWN = {
        21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns",
        80: "http", 110: "pop3", 143: "imap", 443: "https", 445: "smb",
        993: "imaps", 995: "pop3s", 3306: "mysql", 3389: "rdp",
        5432: "postgres", 5900: "vnc", 6379: "redis", 8080: "http-alt",
        8443: "https-alt", 27017: "mongodb",
    }

    sem = asyncio.Semaphore(100)  # Max 100 concurrent connections

    async def _check_port(port: int):
        async with sem:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(target, port),
                    timeout=timeout_s,
                )
                writer.close()
                await writer.wait_closed()
                return {"port": port, "state": "open", "service": WELL_KNOWN.get(port, "")}
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                return {"port": port, "state": "closed", "service": WELL_KNOWN.get(port, "")}

    results = await asyncio.gather(*[_check_port(p) for p in ports])
    open_ports = [r for r in results if r["state"] == "open"]

    return {
        "target": target,
        "scanned_ports": len(ports),
        "open_ports": open_ports,
        "all_ports": sorted(results, key=lambda r: r["port"]),
    }


async def tool_lan_discovery(params: dict) -> dict:
    subnet = _validate_subnet(params.get("subnet", "192.168.1.0/24"))

    rc, stdout, stderr = await _run_subprocess(
        ["nmap", "-sn", subnet, "-oX", "-"],
        timeout=30,
    )

    devices: list[dict] = []
    try:
        root = ET.fromstring(stdout)
        for host in root.findall("host"):
            status = host.find("status")
            if status is not None and status.get("state") != "up":
                continue

            ip_el = host.find("address[@addrtype='ipv4']")
            mac_el = host.find("address[@addrtype='mac']")
            hostname_el = host.find("hostnames/hostname")

            entry: dict[str, Any] = {
                "ip": ip_el.get("addr", "") if ip_el is not None else "",
                "mac": mac_el.get("addr", "") if mac_el is not None else "",
                "vendor": mac_el.get("vendor", "") if mac_el is not None else "",
                "hostname": hostname_el.get("name", "") if hostname_el is not None else "",
            }
            devices.append(entry)
    except ET.ParseError:
        return {"subnet": subnet, "devices": [], "total": 0, "raw": stdout[:2000]}

    return {
        "subnet": subnet,
        "devices": sorted(devices, key=lambda d: [int(o) for o in d["ip"].split(".")] if d["ip"] else []),
        "total": len(devices),
    }


async def tool_iperf_test(params: dict) -> dict:
    server = _validate_hostname(params.get("server", ""))
    if not server:
        raise ValueError("Podaj adres serwera iperf3")
    port = _positive_int(params.get("port", 5201), "port", 1, 65535)
    duration = _positive_int(params.get("duration", 5), "duration", 1, 30)
    direction = params.get("direction", "download")

    cmd = ["iperf3", "-c", server, "-p", str(port), "-t", str(duration), "-J"]
    if direction == "upload":
        pass  # Default is upload in iperf3
    else:
        cmd.append("-R")  # Reverse = download

    rc, stdout, stderr = await _run_subprocess(cmd, timeout=duration + 15)

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise ValueError(f"Blad parsowania wyniku iperf3: {stderr[:500]}")

    end = data.get("end", {})
    sent = end.get("sum_sent", {})
    received = end.get("sum_received", {})
    stream = received if direction == "download" else sent

    return {
        "server": server,
        "port": port,
        "direction": direction,
        "duration_s": stream.get("seconds", duration),
        "transfer_bytes": stream.get("bytes", 0),
        "bandwidth_bps": stream.get("bits_per_second", 0),
        "bandwidth_mbps": round(stream.get("bits_per_second", 0) / 1_000_000, 2),
    }


async def tool_dhcp_leases(params: dict) -> dict:
    # Try reading standard lease files
    lease_files = [
        Path("/var/lib/dhcp/dhclient.leases"),
        Path("/var/lib/dhcpcd/dhcpcd.leases"),
        Path("/var/lib/dhcp/dhclient.eth0.leases"),
        Path("/var/lib/NetworkManager"),
    ]

    leases: list[dict] = []
    source = ""

    for lf in lease_files:
        if lf.is_file():
            source = str(lf)
            content = lf.read_text(errors="replace")
            # Basic parsing of dhclient.leases format
            current: dict[str, str] = {}
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("lease {"):
                    current = {}
                elif line.startswith("fixed-address"):
                    current["ip"] = line.split()[1].rstrip(";")
                elif line.startswith("option dhcp-server-identifier"):
                    current["dhcp_server"] = line.split()[2].rstrip(";")
                elif line.startswith("option domain-name-servers"):
                    current["dns"] = line.split(None, 2)[2].rstrip(";")
                elif line.startswith("option routers"):
                    current["gateway"] = line.split()[2].rstrip(";")
                elif line.startswith("option subnet-mask"):
                    current["subnet_mask"] = line.split()[2].rstrip(";")
                elif line.startswith("expire"):
                    current["expires"] = " ".join(line.split()[1:]).rstrip(";")
                elif line == "}":
                    if current:
                        leases.append(current)
                    current = {}
            break

    # Fallback: show network config from ip commands
    if not leases:
        source = "ip addr / ip route"
        rc_a, stdout_a, _ = await _run_subprocess(["ip", "-j", "addr"], timeout=5)
        rc_r, stdout_r, _ = await _run_subprocess(["ip", "-j", "route"], timeout=5)

        try:
            addrs = json.loads(stdout_a) if stdout_a else []
            routes = json.loads(stdout_r) if stdout_r else []
        except json.JSONDecodeError:
            addrs, routes = [], []

        for iface in addrs:
            ifname = iface.get("ifname", "")
            if ifname == "lo":
                continue
            for addr_info in iface.get("addr_info", []):
                if addr_info.get("family") == "inet":
                    leases.append({
                        "ip": addr_info.get("local", ""),
                        "prefix": str(addr_info.get("prefixlen", "")),
                        "interface": ifname,
                    })

        for route in routes:
            if route.get("dst") == "default":
                leases.append({
                    "type": "default_route",
                    "gateway": route.get("gateway", ""),
                    "interface": route.get("dev", ""),
                })

    return {
        "leases": leases,
        "source": source,
        "count": len(leases),
    }


# ---------------------------------------------------------------------------
# Tool registry & dispatcher
# ---------------------------------------------------------------------------

TOOL_HANDLERS: dict[str, Callable] = {
    "dns_resolve_time": tool_dns_resolve_time,
    "dns_propagation": tool_dns_propagation,
    "dns_leak_test": tool_dns_leak_test,
    "dns_doh_dot_status": tool_dns_doh_dot_status,
    "dns_server_comparison": tool_dns_server_comparison,
    "traceroute": tool_traceroute,
    "mtr": tool_mtr,
    "bgp_as_path": tool_bgp_as_path,
    "reverse_dns": tool_reverse_dns,
    "mtu_discovery": tool_mtu_discovery,
    "geoip": tool_geoip,
    "public_ip": tool_public_ip,
    "nat_type": tool_nat_type,
    "port_scan": tool_port_scan,
    "lan_discovery": tool_lan_discovery,
    "iperf_test": tool_iperf_test,
    "dhcp_leases": tool_dhcp_leases,
}

TOOL_DEFINITIONS: list[dict] = [
    {"name": k, "group": "dns" if k.startswith("dns_") else "routing" if k in ("traceroute", "mtr", "bgp_as_path", "reverse_dns") else "connection" if k in ("mtu_discovery", "geoip", "public_ip", "nat_type") else "local"}
    for k in TOOL_HANDLERS
]

_tool_semaphore = asyncio.Semaphore(3)


async def run_tool(tool_name: str, params: dict) -> dict:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise ValueError(f"Nieznane narzedzie: {tool_name}")

    async with _tool_semaphore:
        t0 = time.perf_counter()
        try:
            result = await handler(params)
            duration_ms = (time.perf_counter() - t0) * 1000
            return {
                "tool": tool_name,
                "status": "ok",
                "duration_ms": round(duration_ms, 1),
                "result": result,
                "error": None,
            }
        except Exception as e:
            duration_ms = (time.perf_counter() - t0) * 1000
            return {
                "tool": tool_name,
                "status": "error",
                "duration_ms": round(duration_ms, 1),
                "result": None,
                "error": str(e),
            }
