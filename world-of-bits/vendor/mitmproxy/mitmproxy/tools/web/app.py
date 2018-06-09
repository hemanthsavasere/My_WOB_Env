
import base64
import hashlib
import json
import logging
import os.path
import re
from io import BytesIO

import tornado.web
import tornado.websocket
import tornado.escape
from mitmproxy import contentviews
from mitmproxy import flowfilter
from mitmproxy import http
from mitmproxy import io
from mitmproxy import version
import mitmproxy.addons.view
import mitmproxy.flow


def convert_flow_to_json_dict(flow: mitmproxy.flow.Flow) -> dict:
    """
    Remove flow message content and cert to save transmission space.

    Args:
        flow: The original flow.
    """
    f = {
        "id": flow.id,
        "intercepted": flow.intercepted,
        "client_conn": flow.client_conn.get_state(),
        "server_conn": flow.server_conn.get_state(),
        "type": flow.type
    }
    if flow.error:
        f["error"] = flow.error.get_state()

    if isinstance(flow, http.HTTPFlow):
        if flow.request:
            f["request"] = {
                "method": flow.request.method,
                "scheme": flow.request.scheme,
                "host": flow.request.host,
                "port": flow.request.port,
                "path": flow.request.path,
                "http_version": flow.request.http_version,
                "headers": tuple(flow.request.headers.items(True)),
                "contentLength": len(flow.request.raw_content) if flow.request.raw_content is not None else None,
                "contentHash": hashlib.sha256(flow.request.raw_content).hexdigest() if flow.request.raw_content is not None else None,
                "timestamp_start": flow.request.timestamp_start,
                "timestamp_end": flow.request.timestamp_end,
                "is_replay": flow.request.is_replay,
            }
        if flow.response:
            f["response"] = {
                "http_version": flow.response.http_version,
                "status_code": flow.response.status_code,
                "reason": flow.response.reason,
                "headers": tuple(flow.response.headers.items(True)),
                "contentLength": len(flow.response.raw_content) if flow.response.raw_content is not None else None,
                "contentHash": hashlib.sha256(flow.response.raw_content).hexdigest() if flow.response.raw_content is not None else None,
                "timestamp_start": flow.response.timestamp_start,
                "timestamp_end": flow.response.timestamp_end,
                "is_replay": flow.response.is_replay,
            }
    f.get("server_conn", {}).pop("cert", None)

    return f


class APIError(tornado.web.HTTPError):
    pass


class BasicAuth:

    def set_auth_headers(self):
        self.set_status(401)
        self.set_header('WWW-Authenticate', 'Basic realm=MITMWeb')
        self._transforms = []
        self.finish()

    def prepare(self):
        wauthenticator = self.application.settings['wauthenticator']
        if wauthenticator:
            auth_header = self.request.headers.get('Authorization')
            if auth_header is None or not auth_header.startswith('Basic '):
                self.set_auth_headers()
            else:
                auth_decoded = base64.decodebytes(auth_header[6:])
                username, password = auth_decoded.split(':', 2)
                if not wauthenticator.test(username, password):
                    self.set_auth_headers()
                    raise APIError(401, "Invalid username or password.")


class RequestHandler(BasicAuth, tornado.web.RequestHandler):

    def write(self, chunk):
        # Writing arrays on the top level is ok nowadays.
        # http://flask.pocoo.org/docs/0.11/security/#json-security
        if isinstance(chunk, list):
            chunk = tornado.escape.json_encode(chunk)
            self.set_header("Content-Type", "application/json; charset=UTF-8")
        super(RequestHandler, self).write(chunk)

    def set_default_headers(self):
        super().set_default_headers()
        self.set_header("Server", version.MITMPROXY)
        self.set_header("X-Frame-Options", "DENY")
        self.add_header("X-XSS-Protection", "1; mode=block")
        self.add_header("X-Content-Type-Options", "nosniff")
        self.add_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "connect-src 'self' ws://* ; "
            "style-src   'self' 'unsafe-inline'"
        )

    @property
    def json(self):
        if not self.request.headers.get("Content-Type").startswith("application/json"):
            return None
        return json.loads(self.request.body.decode())

    @property
    def view(self) -> mitmproxy.addons.view.View:
        return self.application.master.view

    @property
    def master(self) -> "mitmproxy.tools.web.master.WebMaster":
        return self.application.master

    @property
    def flow(self) -> mitmproxy.flow.Flow:
        flow_id = str(self.path_kwargs["flow_id"])
        # FIXME: Add a facility to addon.view to safely access the store
        flow = self.view._store.get(flow_id)
        if flow:
            return flow
        else:
            raise APIError(400, "Flow not found.")

    def write_error(self, status_code: int, **kwargs):
        if "exc_info" in kwargs and isinstance(kwargs["exc_info"][1], APIError):
            self.finish(kwargs["exc_info"][1].log_message)
        else:
            super().write_error(status_code, **kwargs)


class IndexHandler(RequestHandler):

    def get(self):
        token = self.xsrf_token  # https://github.com/tornadoweb/tornado/issues/645
        assert token
        self.render("index.html")


class FilterHelp(RequestHandler):

    def get(self):
        self.write(dict(
            commands=flowfilter.help
        ))


class WebSocketEventBroadcaster(BasicAuth, tornado.websocket.WebSocketHandler):
    # raise an error if inherited class doesn't specify its own instance.
    connections = None  # type: set

    def open(self):
        self.connections.add(self)

    def on_close(self):
        self.connections.remove(self)

    @classmethod
    def broadcast(cls, **kwargs):
        message = json.dumps(kwargs, ensure_ascii=False)

        for conn in cls.connections:
            try:
                conn.write_message(message)
            except Exception:
                logging.error("Error sending message", exc_info=True)


class ClientConnection(WebSocketEventBroadcaster):
    connections = set()  # type: set


class Flows(RequestHandler):

    def get(self):
        self.write([convert_flow_to_json_dict(f) for f in self.view])


class DumpFlows(RequestHandler):
    def get(self):
        self.set_header("Content-Disposition", "attachment; filename=flows")
        self.set_header("Content-Type", "application/octet-stream")

        bio = BytesIO()
        fw = io.FlowWriter(bio)
        for f in self.view:
            fw.add(f)

        self.write(bio.getvalue())
        bio.close()

    def post(self):
        self.view.clear()

        content = self.request.files.values()[0][0].body
        bio = BytesIO(content)
        self.master.load_flows(io.FlowReader(bio).stream())
        bio.close()


class ClearAll(RequestHandler):

    def post(self):
        self.view.clear()


class AcceptFlows(RequestHandler):

    def post(self):
        self.master.accept_all(self.master)


class AcceptFlow(RequestHandler):

    def post(self, flow_id):
        self.flow.resume(self.master)


class FlowHandler(RequestHandler):

    def delete(self, flow_id):
        if self.flow.killable:
            self.flow.kill(self.master)
        self.view.remove(self.flow)

    def put(self, flow_id):
        flow = self.flow
        flow.backup()
        for a, b in self.json.items():
            if a == "request" and hasattr(flow, "request"):
                request = flow.request
                for k, v in b.items():
                    if k in ["method", "scheme", "host", "path", "http_version"]:
                        setattr(request, k, str(v))
                    elif k == "port":
                        request.port = int(v)
                    elif k == "headers":
                        request.headers.clear()
                        for header in v:
                            request.headers.add(*header)
                    elif k == "content":
                        request.text = v
                    else:
                        print("Warning: Unknown update {}.{}: {}".format(a, k, v))

            elif a == "response" and hasattr(flow, "response"):
                response = flow.response
                for k, v in b.items():
                    if k == "msg":
                        response.msg = str(v)
                    elif k == "code":
                        response.status_code = int(v)
                    elif k == "http_version":
                        response.http_version = str(v)
                    elif k == "headers":
                        response.headers.clear()
                        for header in v:
                            response.headers.add(*header)
                    elif k == "content":
                        response.text = v
                    else:
                        print("Warning: Unknown update {}.{}: {}".format(a, k, v))
            else:
                print("Warning: Unknown update {}: {}".format(a, b))
        self.view.update(flow)


class DuplicateFlow(RequestHandler):

    def post(self, flow_id):
        self.master.view.duplicate_flow(self.flow)


class RevertFlow(RequestHandler):

    def post(self, flow_id):
        self.flow.revert()


class ReplayFlow(RequestHandler):

    def post(self, flow_id):
        self.flow.backup()
        self.flow.response = None
        self.view.update(self.flow)

        r = self.master.replay_request(self.flow)
        if r:
            raise APIError(400, r)


class FlowContent(RequestHandler):

    def post(self, flow_id, message):
        self.flow.backup()
        message = getattr(self.flow, message)
        message.content = self.request.files.values()[0][0].body
        self.view.update(self.flow)

    def get(self, flow_id, message):
        message = getattr(self.flow, message)

        if not message.raw_content:
            raise APIError(400, "No content.")

        content_encoding = message.headers.get("Content-Encoding", None)
        if content_encoding:
            content_encoding = re.sub(r"[^\w]", "", content_encoding)
            self.set_header("Content-Encoding", content_encoding)

        original_cd = message.headers.get("Content-Disposition", None)
        filename = None
        if original_cd:
            filename = re.search('filename=([-\w" .()]+)', original_cd)
            if filename:
                filename = filename.group(1)
        if not filename:
            filename = self.flow.request.path.split("?")[0].split("/")[-1]

        filename = re.sub(r'[^-\w" .()]', "", filename)
        cd = "attachment; filename={}".format(filename)
        self.set_header("Content-Disposition", cd)
        self.set_header("Content-Type", "application/text")
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("X-Frame-Options", "DENY")
        self.write(message.raw_content)


class FlowContentView(RequestHandler):

    def get(self, flow_id, message, content_view):
        message = getattr(self.flow, message)

        description, lines, error = contentviews.get_message_content_view(
            content_view.replace('_', ' '), message
        )
#        if error:
#           add event log

        self.write(dict(
            lines=list(lines),
            description=description
        ))


class Events(RequestHandler):

    def get(self):
        self.write([])  # FIXME


class Settings(RequestHandler):

    def get(self):
        self.write(dict(
            version=version.VERSION,
            mode=str(self.master.options.mode),
            intercept=self.master.options.intercept,
            showhost=self.master.options.showhost,
            no_upstream_cert=self.master.options.no_upstream_cert,
            rawtcp=self.master.options.rawtcp,
            http2=self.master.options.http2,
            anticache=self.master.options.anticache,
            anticomp=self.master.options.anticomp,
            stickyauth=self.master.options.stickyauth,
            stickycookie=self.master.options.stickycookie,
            stream=self.master.options.stream_large_bodies,
            contentViews=[v.name.replace(' ', '_') for v in contentviews.views]
        ))

    def put(self):
        update = {}
        for k, v in self.json.items():
            if k == "intercept":
                self.master.options.intercept = v
                update[k] = v
            elif k == "showhost":
                self.master.options.showhost = v
                update[k] = v
            elif k == "no_upstream_cert":
                self.master.options.no_upstream_cert = v
                update[k] = v
            elif k == "rawtcp":
                self.master.options.rawtcp = v
                update[k] = v
            elif k == "http2":
                self.master.options.http2 = v
                update[k] = v
            elif k == "anticache":
                self.master.options.anticache = v
                update[k] = v
            elif k == "anticomp":
                self.master.options.anticomp = v
                update[k] = v
            elif k == "stickycookie":
                self.master.options.stickycookie = v
                update[k] = v
            elif k == "stickyauth":
                self.master.options.stickyauth = v
                update[k] = v
            elif k == "stream":
                self.master.options.stream_large_bodies = v
                update[k] = v
            else:
                print("Warning: Unknown setting {}: {}".format(k, v))

        ClientConnection.broadcast(
            resource="settings",
            cmd="update",
            data=update
        )


class Application(tornado.web.Application):

    def __init__(self, master, debug, wauthenticator):
        self.master = master
        handlers = [
            (r"/", IndexHandler),
            (r"/filter-help", FilterHelp),
            (r"/updates", ClientConnection),
            (r"/events", Events),
            (r"/flows", Flows),
            (r"/flows/dump", DumpFlows),
            (r"/flows/accept", AcceptFlows),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)", FlowHandler),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/accept", AcceptFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/duplicate", DuplicateFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/replay", ReplayFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/revert", RevertFlow),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/(?P<message>request|response)/content", FlowContent),
            (r"/flows/(?P<flow_id>[0-9a-f\-]+)/(?P<message>request|response)/content/(?P<content_view>[0-9a-zA-Z\-\_]+)", FlowContentView),
            (r"/settings", Settings),
            (r"/clear", ClearAll),
        ]
        settings = dict(
            template_path=os.path.join(os.path.dirname(__file__), "templates"),
            static_path=os.path.join(os.path.dirname(__file__), "static"),
            xsrf_cookies=True,
            cookie_secret=os.urandom(256),
            debug=debug,
            autoreload=False,
            wauthenticator=wauthenticator,
        )
        super().__init__(handlers, **settings)
