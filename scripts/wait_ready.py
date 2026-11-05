import argparse
import socket
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(1)
                conn.connect(args.socket)
                conn.sendall(b"GET /_ext/healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
                if b"200 OK" in conn.recv(4096):
                    return
        except OSError:
            pass
        time.sleep(0.2)
    parser.exit(1, "Extension listener did not become ready\n")


if __name__ == "__main__":
    main()
