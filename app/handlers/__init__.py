from app.handlers import flow, start


def get_routers():
    return [start.router, flow.router]
