"""Explicit proxy trust, origin checks and HTTPS cookie behavior; offline only."""
import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from studio.web.__main__ import trusted_proxy_ips
from studio.web.api import create_app


def test_proxy_trust_requires_explicit_addresses():
    assert trusted_proxy_ips('') == ''
    assert trusted_proxy_ips('127.0.0.1, ::1') == '127.0.0.1,::1'
    for value in ['*', '0.0.0.0/0', 'https://proxy.example', '127.0.0.1,evil.example']:
        with pytest.raises(ValueError):
            trusted_proxy_ips(value)


def test_trusted_tls_proxy_supports_secure_sessions(tmp_path):
    token='only-a-test-token-for-proxy-tests'
    app=create_app(tmp_path,host='0.0.0.0',token=token,allowed_hosts=['studio.example'])
    wrapped=ProxyHeadersMiddleware(app,trusted_hosts='127.0.0.1')
    with TestClient(wrapped,base_url='http://studio.example',client=('127.0.0.1',1234)) as client:
        headers={'origin':'https://studio.example','x-forwarded-proto':'https'}
        response=client.post('/api/session',json={'token':token},headers=headers)
        assert response.status_code==200
        assert '; Secure' in response.headers['set-cookie']
        response=client.post('/api/projects',json={'name':'Proxy story'},
                             headers={**headers,'authorization':'Bearer '+token})
        assert response.status_code==200
        response=client.post('/api/projects',json={'name':'Foreign'},headers={
            **headers,'origin':'https://attacker.example','authorization':'Bearer '+token})
        assert response.status_code==403


def test_untrusted_client_cannot_spoof_tls(tmp_path):
    app=create_app(tmp_path,host='127.0.0.1',token=None,allowed_hosts=['studio.example'])
    wrapped=ProxyHeadersMiddleware(app,trusted_hosts='127.0.0.1')
    with TestClient(wrapped,base_url='http://studio.example',client=('203.0.113.7',1234)) as client:
        response=client.post('/api/projects',json={'name':'Spoof'},headers={
            'origin':'https://studio.example','x-forwarded-proto':'https'})
        assert response.status_code==403
