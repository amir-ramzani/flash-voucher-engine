"""Generate the Postman collection + local environment.

    python -m scripts.build_postman      # writes docs/postman/*.json

The collection is a runnable happy-path flow (Collection Runner: top to bottom) plus a
folder of negative cases. In `authMode=dev` a collection-level pre-request script signs
HS256 dev tokens with `devJwtSecret` (CryptoJS), so no token copy-pasting is needed.
In `authMode=cognito`, paste real access tokens into `userToken` / `adminToken`.
"""

import json
from pathlib import Path

from api.app import API_VERSION

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "postman"
SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

AUTH_SCRIPT = r"""
// Mint dev tokens (AUTH_MODE=dev on the server). Skipped when authMode=cognito.
function b64url(words) {
  return CryptoJS.enc.Base64.stringify(words)
    .replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
}
function mint(sub, groups) {
  const now = Math.floor(Date.now() / 1000);
  const enc = (o) => b64url(CryptoJS.enc.Utf8.parse(JSON.stringify(o)));
  const head = enc({ alg: 'HS256', typ: 'JWT' });
  const body = enc({ sub: sub, iat: now, exp: now + 3600, 'cognito:groups': groups });
  const sig = b64url(CryptoJS.HmacSHA256(head + '.' + body, pm.variables.get('devJwtSecret')));
  return head + '.' + body + '.' + sig;
}
if ((pm.variables.get('authMode') || 'dev') === 'dev') {
  pm.variables.set('userToken', mint(pm.variables.get('userId'), []));
  pm.variables.set('otherUserToken', mint(pm.variables.get('userId') + '-other', []));
  pm.variables.set('adminToken', mint(pm.variables.get('adminId'), ['admin']));
}
""".strip()


def js(src: str) -> list[str]:
    return src.strip().splitlines()


def event(listen: str, src: str) -> dict:
    return {"listen": listen, "script": {"type": "text/javascript", "exec": js(src)}}


def bearer(var: str) -> dict:
    return {"type": "bearer", "bearer": [{"key": "token", "value": f"{{{{{var}}}}}"}]}


def request(
    name: str,
    method: str,
    path: list[str],
    *,
    token: str | None = "userToken",
    body: dict | str | None = None,
    test: str = "",
    pre: str = "",
    description: str = "",
) -> dict:
    req: dict = {
        "method": method,
        "header": [{"key": "Accept", "value": "application/json"}],
        "url": {
            "raw": "{{baseUrl}}/" + "/".join(path),
            "host": ["{{baseUrl}}"],
            "path": path,
        },
        "description": description,
        "auth": bearer(token) if token else {"type": "noauth"},
    }
    if body is not None:
        req["header"].append({"key": "Content-Type", "value": "application/json"})
        req["body"] = {
            "mode": "raw",
            "raw": body if isinstance(body, str) else json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    item: dict = {"name": name, "request": req, "event": []}
    if pre:
        item["event"].append(event("prerequest", pre))
    if test:
        item["event"].append(event("test", test))
    return item


def collection() -> dict:
    campaign = ["campaigns", "{{campaignId}}"]
    happy = [
        request(
            "Health",
            "GET",
            ["health"],
            token=None,
            test="""
pm.test('200 ok', () => { pm.response.to.have.status(200); pm.expect(pm.response.json().ok).to.eql(true); });
""",
        ),
        request(
            "Create campaign (admin)",
            "POST",
            ["campaigns"],
            token="adminToken",
            description=(
                "Creates a fresh campaign each run (random `campaignId`, window = now-5s … "
                "now+30m) so the flow is re-runnable. Set `totalStock` to taste."
            ),
            # Raw text so {{totalStock}} lands as a JSON number, not a string.
            body=(
                '{\n  "campaignId": "{{campaignId}}",\n  "totalStock": {{totalStock}},\n'
                '  "startAt": "{{startAt}}",\n  "endAt": "{{endAt}}"\n}'
            ),
            pre="""
const id = 'pm-' + Date.now().toString(36);
pm.collectionVariables.set('campaignId', id);
pm.collectionVariables.set('startAt', new Date(Date.now() - 5000).toISOString());
pm.collectionVariables.set('endAt', new Date(Date.now() + 30 * 60 * 1000).toISOString());
""",
            test="""
pm.test('201 created', () => pm.response.to.have.status(201));
pm.test('stock seeded', () => pm.expect(pm.response.json().remainingStock).to.eql(Number(pm.variables.get('totalStock'))));
""",
        ),
        request(
            "Claim voucher",
            "POST",
            [*campaign, "claim"],
            test="""
pm.test('200', () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test('WON', () => pm.expect(b.result).to.eql('WON'));
if (b.voucherCode) pm.collectionVariables.set('voucherCode', b.voucherCode);
""",
        ),
        request(
            "Claim again (duplicate tap)",
            "POST",
            [*campaign, "claim"],
            description="Retries are safe: same code, no extra voucher.",
            test="""
const b = pm.response.json();
pm.test('ALREADY', () => pm.expect(b.result).to.eql('ALREADY'));
pm.test('same code', () => pm.expect(b.voucherCode).to.eql(pm.collectionVariables.get('voucherCode')));
""",
        ),
        request(
            "My voucher",
            "GET",
            [*campaign, "my-voucher"],
            description="`PENDING` until the worker persists the claim (normally a few ms).",
            pre="setTimeout(() => {}, 300); // give the worker a moment",
            test="""
pm.test('200', () => pm.response.to.have.status(200));
pm.test('PENDING or ISSUED', () => pm.expect(pm.response.json().status).to.be.oneOf(['PENDING', 'ISSUED']));
""",
        ),
        request(
            "Redeem voucher",
            "POST",
            ["vouchers", "{{voucherCode}}", "redeem"],
            pre="setTimeout(() => {}, 300);",
            test="""
pm.test('200 REDEEMED', () => { pm.response.to.have.status(200); pm.expect(pm.response.json().status).to.eql('REDEEMED'); });
""",
        ),
        request(
            "Redeem again (must fail)",
            "POST",
            ["vouchers", "{{voucherCode}}", "redeem"],
            test="pm.test('409 already redeemed', () => pm.response.to.have.status(409));",
        ),
    ]
    negative = [
        request(
            "Claim without token → 401",
            "POST",
            [*campaign, "claim"],
            token=None,
            test="pm.test('401', () => pm.response.to.have.status(401));",
        ),
        request(
            "Create campaign as non-admin → 403",
            "POST",
            ["campaigns"],
            body={
                "campaignId": "forbidden",
                "totalStock": 1,
                "startAt": "2030-01-01T00:00:00Z",
                "endAt": "2030-01-01T01:00:00Z",
            },
            test="pm.test('403', () => pm.response.to.have.status(403));",
        ),
        request(
            "Claim unknown campaign → 404",
            "POST",
            ["campaigns", "does-not-exist", "claim"],
            test="pm.test('404', () => pm.response.to.have.status(404));",
        ),
        request(
            "Redeem someone else's voucher → 404",
            "POST",
            ["vouchers", "{{voucherCode}}", "redeem"],
            token="otherUserToken",
            test="pm.test('404', () => pm.response.to.have.status(404));",
        ),
        request(
            "Invalid campaign body → 422",
            "POST",
            ["campaigns"],
            token="adminToken",
            body={"campaignId": "Bad.Id", "totalStock": 0, "startAt": "x", "endAt": "y"},
            test="pm.test('422', () => pm.response.to.have.status(422));",
        ),
    ]
    variables = {
        "baseUrl": "http://localhost:8000",
        "authMode": "dev",
        "devJwtSecret": "dev-secret-change-me-at-least-32-bytes!",
        "userId": "postman-user",
        "adminId": "postman-admin",
        "totalStock": "100",
        "campaignId": "",
        "startAt": "",
        "endAt": "",
        "voucherCode": "",
        "userToken": "",
        "otherUserToken": "",
        "adminToken": "",
    }
    return {
        "info": {
            "name": "Flash-Sale Voucher Engine",
            "version": API_VERSION,
            "description": (
                "Run top-to-bottom with the Collection Runner. `authMode=dev` signs tokens "
                "locally with `devJwtSecret` (server must run `AUTH_MODE=dev`). For Cognito, "
                "set `authMode=cognito` and paste access tokens into `userToken`, "
                "`otherUserToken` and `adminToken`. Change `userId` to act as another user. "
                "Full spec: docs/openapi.yaml."
            ),
            "schema": SCHEMA,
        },
        "auth": bearer("userToken"),
        "event": [event("prerequest", AUTH_SCRIPT)],
        "variable": [{"key": k, "value": v} for k, v in variables.items()],
        "item": [
            {"name": "1. Happy path (run in order)", "item": happy},
            {"name": "2. Negative cases", "item": negative},
        ],
    }


def environment() -> dict:
    return {
        "name": "flash-voucher local",
        "values": [
            {"key": "baseUrl", "value": "http://localhost:8000", "enabled": True},
            {"key": "authMode", "value": "dev", "enabled": True},
            {
                "key": "devJwtSecret",
                "value": "dev-secret-change-me-at-least-32-bytes!",
                "type": "secret",
                "enabled": True,
            },
        ],
        "_postman_variable_scope": "environment",
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "flash-voucher.postman_collection.json").write_text(
        json.dumps(collection(), indent=2) + "\n"
    )
    (OUT / "local.postman_environment.json").write_text(json.dumps(environment(), indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
