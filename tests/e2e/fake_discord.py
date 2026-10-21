"""A stand-in for Discord in the browser tests (never used outside tests).

/oauth2/authorize  answers like Discord after "Authorize": redirect to redirect_uri with
                   #access_token=...&state=... (always logs in as Lier)
/api/users/@me     knows two tokens, everything else is 401
Run: python -m uvicorn tests.e2e.fake_discord:app --port 8769
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

LIER, SAM = "tok-lier-0123456789", "tok-sam-0123456789"
USERS = {LIER: {"id": "1001", "global_name": "Lier"}, SAM: {"id": "1002", "global_name": "Sam"}}

app = FastAPI()


@app.get("/oauth2/authorize")
async def authorize(redirect_uri: str, state: str, response_type: str, scope: str, client_id: str):
    assert response_type == "token" and scope == "identify"
    return RedirectResponse(f"{redirect_uri}#token_type=Bearer&access_token={LIER}&expires_in=604800"
                            f"&scope=identify&state={state}")


@app.get("/api/users/@me")
async def me(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token in USERS:
        return USERS[token]
    return JSONResponse({"message": "401: Unauthorized"}, status_code=401)
