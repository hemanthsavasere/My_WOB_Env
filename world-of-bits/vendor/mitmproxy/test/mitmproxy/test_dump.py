from mitmproxy.test import tflow
import os
import io

from mitmproxy.tools import dump
from mitmproxy import exceptions
from mitmproxy import proxy
from mitmproxy.test import tutils
from . import mastertest


class TestDumpMaster(mastertest.MasterTest):
    def dummy_cycle(self, master, n, content):
        mastertest.MasterTest.dummy_cycle(self, master, n, content)
        return master.options.tfile.getvalue()

    def mkmaster(self, flt, **options):
        if "verbosity" not in options:
            options["verbosity"] = 0
        if "flow_detail" not in options:
            options["flow_detail"] = 0
        o = dump.Options(filtstr=flt, tfile=io.StringIO(), **options)
        return dump.DumpMaster(o, proxy.DummyServer())

    def test_basic(self):
        for i in (1, 2, 3):
            assert "GET" in self.dummy_cycle(
                self.mkmaster("~s", flow_detail=i),
                1,
                b""
            )
            assert "GET" in self.dummy_cycle(
                self.mkmaster("~s", flow_detail=i),
                1,
                b"\x00\x00\x00"
            )
            assert "GET" in self.dummy_cycle(
                self.mkmaster("~s", flow_detail=i),
                1,
                b"ascii"
            )

    def test_error(self):
        o = dump.Options(
            tfile=io.StringIO(),
            flow_detail=1
        )
        m = dump.DumpMaster(o, proxy.DummyServer())
        f = tflow.tflow(err=True)
        m.error(f)
        assert "error" in o.tfile.getvalue()

    def test_replay(self):
        o = dump.Options(server_replay=["nonexistent"], replay_kill_extra=True)
        tutils.raises(exceptions.OptionsError, dump.DumpMaster, o, proxy.DummyServer())

        with tutils.tmpdir() as t:
            p = os.path.join(t, "rep")
            self.flowfile(p)

            o = dump.Options(server_replay=[p], replay_kill_extra=True)
            o.verbosity = 0
            o.flow_detail = 0
            m = dump.DumpMaster(o, proxy.DummyServer())

            self.cycle(m, b"content")
            self.cycle(m, b"content")

            o = dump.Options(server_replay=[p], replay_kill_extra=False)
            o.verbosity = 0
            o.flow_detail = 0
            m = dump.DumpMaster(o, proxy.DummyServer())
            self.cycle(m, b"nonexistent")

            o = dump.Options(client_replay=[p], replay_kill_extra=False)
            o.verbosity = 0
            o.flow_detail = 0
            m = dump.DumpMaster(o, proxy.DummyServer())

    def test_read(self):
        with tutils.tmpdir() as t:
            p = os.path.join(t, "read")
            self.flowfile(p)
            assert "GET" in self.dummy_cycle(
                self.mkmaster(None, flow_detail=1, rfile=p),
                1, b"",
            )
            tutils.raises(
                dump.DumpError,
                self.mkmaster, None, verbosity=1, rfile="/nonexistent"
            )
            tutils.raises(
                dump.DumpError,
                self.mkmaster, None, verbosity=1, rfile="test_dump.py"
            )

    def test_options(self):
        o = dump.Options(verbosity = 2)
        assert o.verbosity == 2

    def test_filter(self):
        assert "GET" not in self.dummy_cycle(
            self.mkmaster("~u foo", verbosity=1), 1, b""
        )

    def test_replacements(self):
        o = dump.Options(
            replacements=[(".*", "content", "foo")],
            tfile = io.StringIO(),
        )
        o.verbosity = 0
        o.flow_detail = 0
        m = dump.DumpMaster(o, proxy.DummyServer())
        f = self.cycle(m, b"content")
        assert f.request.content == b"foo"

    def test_setheader(self):
        o = dump.Options(
            setheaders=[(".*", "one", "two")],
            tfile=io.StringIO()
        )
        o.verbosity = 0
        o.flow_detail = 0
        m = dump.DumpMaster(o, proxy.DummyServer())
        f = self.cycle(m, b"content")
        assert f.request.headers["one"] == "two"

    def test_script(self):
        ret = self.dummy_cycle(
            self.mkmaster(
                None,
                scripts=[tutils.test_data.path("mitmproxy/data/scripts/all.py")],
                verbosity=2
            ),
            1, b"",
        )
        assert "XCLIENTCONNECT" in ret
        assert "XSERVERCONNECT" in ret
        assert "XREQUEST" in ret
        assert "XRESPONSE" in ret
        assert "XCLIENTDISCONNECT" in ret
        tutils.raises(
            exceptions.AddonError,
            self.mkmaster,
            None, scripts=["nonexistent"]
        )
        tutils.raises(
            exceptions.AddonError,
            self.mkmaster,
            None, scripts=["starterr.py"]
        )

    def test_stickycookie(self):
        self.dummy_cycle(
            self.mkmaster(None, stickycookie = ".*"),
            1, b""
        )

    def test_stickyauth(self):
        self.dummy_cycle(
            self.mkmaster(None, stickyauth = ".*"),
            1, b""
        )
