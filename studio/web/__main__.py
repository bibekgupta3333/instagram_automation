"""Run the local app: python -m studio.web --port 8080."""
import argparse
import ipaddress
import os


def trusted_proxy_ips(value: str) -> str:
    """Only explicit proxy IPs; never trust arbitrary forwarded headers."""
    addresses = [part.strip() for part in value.split(",") if part.strip()]
    if len(addresses) > 8:
        raise ValueError("At most eight trusted proxy addresses are allowed")
    for address in addresses:
        ipaddress.ip_address(address)
    return ",".join(addresses)


def main():
    parser = argparse.ArgumentParser(description="Local Video Studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    proxies = trusted_proxy_ips(os.environ.get("STUDIO_WEB_TRUSTED_PROXY_IPS", ""))
    import uvicorn

    from .api import create_app
    app = create_app(host=args.host, token=os.environ.get("STUDIO_WEB_TOKEN"))
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=bool(proxies),
                forwarded_allow_ips=proxies)


if __name__ == "__main__":
    main()
