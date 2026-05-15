import concurrent.futures
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from core.ssh import get_client_with_key, run_command

router = APIRouter()


class PreflightRequest(BaseModel):
    nodes: List[dict]


def _check_node(ip: str, ssh_user: str, hostname: str) -> dict:
    checks = {}
    client = None

    try:
        client = get_client_with_key(ip, ssh_user)
    except Exception as exc:
        return {
            "hostname": hostname,
            "ip": ip,
            "status": "error",
            "checks": {
                "SSH access": (
                    "FAIL - Could not connect with the saved SSH key. "
                    "Check the IP address, SSH user, and whether Bootstrap SSH "
                    f"completed successfully. Details: {exc}"
                )
            },
        }

    def run(cmd):
        out, _, rc = run_command(client, cmd)
        return out, rc

    checks["SSH access"] = "OK - SSH key access is working."

    out, _ = run("lsb_release -rs")
    checks["Operating system"] = (
        f"OK - Ubuntu {out} detected."
        if out in ("22.04", "24.04")
        else f"FAIL - This machine is using Ubuntu {out}. This platform supports Ubuntu 22.04 or 24.04 only."
    )

    out, _ = run("free -m | awk '/^Mem:/{print $2}'")
    try:
        ram_mb = int(out)
        if ram_mb < 2048:
            checks["Memory"] = (
                f"FAIL - This machine has {ram_mb} MB of RAM. At least 2000 MB is required to continue."
            )
        elif ram_mb < 4096:
            checks["Memory"] = (
                f"WARN - This machine has {ram_mb} MB of RAM. Installation can continue, "
                "but 4096 MB or more is recommended for a more comfortable cluster setup."
            )
        else:
            checks["Memory"] = f"OK - {ram_mb} MB of RAM detected."
    except ValueError:
        checks["Memory"] = "FAIL - Could not read the machine's RAM amount."

    out, _ = run("df -m / | awk 'NR==2{print $4}'")
    try:
        disk_mb = int(out)
        checks["Free disk space"] = (
            f"OK - {disk_mb} MB of free disk space is available."
            if disk_mb >= 10240
            else f"FAIL - This machine has {disk_mb} MB of free disk space. At least 20000 MB free is required."
        )
    except ValueError:
        checks["Free disk space"] = "FAIL - Could not read the machine's free disk space."

    out, _ = run("swapon --show")
    checks["Swap"] = (
        "OK - Swap is disabled."
        if out == ""
        else (
            "WARN - Swap is enabled. The setup will try to disable it, but "
            "leaving swap on can still cause installation or stability issues."
        )
    )

    out, _ = run("python3 --version")
    checks["Python 3"] = (
        f"OK - {out} is installed."
        if out.startswith("Python 3")
        else "FAIL - Python 3 was not found. Install Python 3 before continuing."
    )

    out, _ = run("test -f /etc/kubernetes/admin.conf && echo exists || echo absent")
    checks["Kubernetes clean state"] = (
        "OK - No previous Kubernetes control-plane state was found."
        if out == "absent"
        else (
            "FAIL - Existing Kubernetes state was found on this machine. "
            "Clean it first before using this launcher for a fresh installation."
        )
    )

    out, rc = run("sudo -n whoami")
    checks["Passwordless sudo"] = (
        "OK - Passwordless sudo is working."
        if rc == 0 and out.strip() == "root"
        else (
            "FAIL - This machine is not ready for passwordless sudo. "
            "Re-run Bootstrap SSH for this machine before continuing."
        )
    )

    out, _ = run("hostname")
    checks["Hostname alignment"] = (
        f"OK - The machine hostname matches Bootstrap ({out})."
        if out.strip() == hostname.strip()
        else (
            f"FAIL - This machine currently identifies itself as '{out}', but Bootstrap expected '{hostname}'. "
            "Go back to Bootstrap and correct the hostname there, or rename the machine so both names match."
        )
    )

    out, rc = run(f"grep -c '^127\\.0\\.1\\.1 {hostname}$' /etc/hosts")
    checks["Local hostname mapping"] = (
        f"OK - /etc/hosts contains 127.0.1.1 {hostname}."
        if rc == 0 and out.strip() != "0"
        else (
            f"FAIL - The local /etc/hosts entry for '{hostname}' is missing. "
            "This machine may not resolve its own hostname correctly. Re-run Bootstrap SSH or fix the machine hostname configuration."
        )
    )

    out, rc = run("test -s ~/.ssh/authorized_keys && echo ok || echo missing")
    checks["Authorized SSH key"] = (
        "OK - The SSH authorized_keys file is present."
        if out.strip() == "ok"
        else "FAIL - The SSH authorized_keys file is missing or empty. Re-run Bootstrap SSH for this machine."
    )

    if client:
        client.close()

    failed = [key for key, value in checks.items() if value.startswith("FAIL")]
    warnings = [key for key, value in checks.items() if value.startswith("WARN")]
    return {
        "hostname": hostname,
        "ip": ip,
        "status": "error" if failed else ("warn" if warnings else "ok"),
        "checks": checks,
    }


@router.post("/api/preflight")
async def preflight(request: PreflightRequest):
    results = []
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(
                _check_node,
                node["ip"],
                node["ssh_user"],
                node["hostname"],
            ): node
            for node in request.nodes
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    os_versions = {}
    for result in results:
        os_text = result["checks"].get("Operating system", "")
        if os_text.startswith("OK - Ubuntu "):
            version = os_text.replace("OK - Ubuntu ", "").replace(" detected.", "")
            os_versions.setdefault(version, []).append(result["hostname"])

    cluster_checks = {}
    if len(os_versions) == 1:
        version = next(iter(os_versions.keys()))
        cluster_checks["Cluster OS consistency"] = (
            f"OK - All selected machines are running Ubuntu {version}."
        )
    elif len(os_versions) > 1:
        version_summary = ", ".join(
            f"Ubuntu {version}: {', '.join(sorted(hostnames))}"
            for version, hostnames in sorted(os_versions.items())
        )
        cluster_checks["Cluster OS consistency"] = (
            "FAIL - The selected machines are not using the same Ubuntu version. "
            f"Mixed OS versions are not supported in this platform. Detected: {version_summary}."
        )
    else:
        cluster_checks["Cluster OS consistency"] = (
            "FAIL - Could not confirm a supported Ubuntu version on all selected machines."
        )

    failed = [result for result in results if result["status"] == "error"]
    warnings = [result for result in results if result["status"] == "warn"]
    cluster_failed = [key for key, value in cluster_checks.items() if value.startswith("FAIL")]

    return {
        "status": "error" if failed or cluster_failed else ("warn" if warnings else "ok"),
        "cluster_checks": cluster_checks,
        "results": sorted(results, key=lambda result: result["hostname"]),
    }
