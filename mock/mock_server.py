"""Fake versions of the 4 APIs so you can test the chain before touching real envs.
   python mock/mock_server.py   -> http://127.0.0.1:9090   (then APICHAIN_ENV=local)"""
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

DB = {"ind": {}, "cust": {}, "acct": {}, "ord": {}}
ACCOUNT_CALLS = {"n": 0}


def _auth(req):
    return req.headers.get("authorization", "").startswith("Bearer ")


async def individuals(req):
    if not _auth(req):
        return JSONResponse({"error": "unauthorized"}, 401)
    b = await req.json()
    i = "IND-" + uuid.uuid4().hex[:8]
    DB["ind"][i] = b
    return JSONResponse({"id": i, **b}, 201)


async def customers(req):
    b = await req.json()
    if b.get("individualId") not in DB["ind"]:
        return JSONResponse({"error": "individual not found"}, 404)
    c = "CUS-" + uuid.uuid4().hex[:8]
    DB["cust"][c] = b
    return JSONResponse({"customerId": c, "individualId": b["individualId"]}, 201)


async def accounts(req):
    b = await req.json()
    ACCOUNT_CALLS["n"] += 1
    if ACCOUNT_CALLS["n"] % 2 == 1:  # simulate eventual consistency: first try fails
        return JSONResponse({"error": "customer not yet replicated"}, 409)
    if b.get("customerId") not in DB["cust"]:
        return JSONResponse({"error": "customer not found"}, 404)
    a = "ACC-" + uuid.uuid4().hex[:8]
    DB["acct"][a] = b
    return JSONResponse({"account": {"id": a, "status": "ACTIVE"}}, 201)


async def orders(req):
    b = await req.json()
    if b.get("accountId") not in DB["acct"]:
        return JSONResponse({"error": "account not found"}, 404)
    o = "ORD-" + uuid.uuid4().hex[:8]
    DB["ord"][o] = b
    return JSONResponse({"orderId": o, "status": "SUBMITTED"}, 202, headers={"Location": f"/v1/orders/{o}"})


async def get_order(req):
    o = req.path_params["oid"]
    return JSONResponse({"orderId": o, **DB["ord"][o]} if o in DB["ord"] else {"error": "nf"},
                        200 if o in DB["ord"] else 404)


app = Starlette(routes=[
    Route("/v1/individuals", individuals, methods=["POST"]),
    Route("/v1/customers", customers, methods=["POST"]),
    Route("/v1/accounts", accounts, methods=["POST"]),
    Route("/v1/orders", orders, methods=["POST"]),
    Route("/v1/orders/{oid}", get_order, methods=["GET"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9090, log_level="warning")
