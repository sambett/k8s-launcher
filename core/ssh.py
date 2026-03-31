import socket
import paramiko
from core.paths import SSH_KEY_PATH


def get_client_with_password(ip: str, ssh_user: str, ssh_pass: str,
                              timeout: int = 15) -> paramiko.SSHClient:
    """
    Open an SSH connection using username + password.
    Used only during bootstrap (one-time key push).
    Caller is responsible for closing the client.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=ip,
        username=ssh_user,
        password=ssh_pass,
        timeout=timeout,
        look_for_keys=False,
        allow_agent=False
    )
    return client


def get_client_with_key(ip: str, ssh_user: str,
                         timeout: int = 10) -> paramiko.SSHClient:
    """
    Open an SSH connection using the ed25519 private key.
    Used by preflight, validate, and any post-bootstrap operations.
    Caller is responsible for closing the client.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=ip,
        username=ssh_user,
        key_filename=str(SSH_KEY_PATH),
        timeout=timeout,
        look_for_keys=False,
        allow_agent=False
    )
    return client


def run_command(client: paramiko.SSHClient, cmd: str) -> tuple:
    """
    Run a single command on an open SSH client.
    Returns (stdout_str, stderr_str, exit_code).
    """
    _, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    return (
        stdout.read().decode().strip(),
        stderr.read().decode().strip(),
        exit_code
    )
