"""Unit tests for codex-cua tree parsing. Run: python3 -m unittest discover tests"""

import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_loader(
    "codex_cua",
    importlib.machinery.SourceFileLoader("codex_cua", str(Path(__file__).resolve().parents[1] / "bin" / "codex-cua")),
)
cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cua)

# Two renderings of the same Calculator window: unfocused (terse) and focused
# (descriptions present). Element numbering differs between the snapshots, which
# is why --match resolves against a fresh tree instead of a remembered index.
UNFOCUSED = """Computer Use state
<app_state>
App=com.apple.calculator (pid 21639)
0 unknown Secondary Actions: Raise
\t1 split group main, SidebarNavigationSplitView
\t\t21 button Seven
\t\t22 button Eight
\t\t23 button Nine
"""

FOCUSED = """Computer Use state
0 standard window Calculator, ID: main
\t\t\t21 button Description: 7, ID: Seven
\t\t\t22 button Description: 8, ID: Eight
\t\t\t23 button Description: 9, ID: Nine
"""


class TreeMatchesTest(unittest.TestCase):
    def test_returns_index_and_line(self):
        self.assertEqual(cua.tree_matches(UNFOCUSED, "Nine"), [("23", "23 button Nine")])

    def test_matches_both_renderings(self):
        for tree in (UNFOCUSED, FOCUSED):
            self.assertEqual([i for i, _ in cua.tree_matches(tree, "Nine")], ["23"])

    def test_button_prefix_only_matches_terse_rendering(self):
        self.assertTrue(cua.tree_matches(UNFOCUSED, "button Nine"))
        self.assertFalse(cua.tree_matches(FOCUSED, "button Nine"))

    def test_skips_lines_without_an_element_index(self):
        self.assertEqual(cua.tree_matches(UNFOCUSED, "calculator"), [])

    def test_case_sensitivity_is_opt_in(self):
        self.assertTrue(cua.tree_matches(UNFOCUSED, "nine"))
        self.assertFalse(cua.tree_matches(UNFOCUSED, "nine", case_sensitive=True))

    def test_reports_every_hit_so_ambiguity_can_be_detected(self):
        self.assertEqual(len(cua.tree_matches(UNFOCUSED, "button")), 3)

    def test_bad_pattern_fails_loudly(self):
        with self.assertRaises(SystemExit):
            cua.tree_matches(UNFOCUSED, "button (")


class ParsePointTest(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(cua.parse_point("12, 34.5", "--from"), (12.0, 34.5))

    def test_rejects_wrong_arity(self):
        with self.assertRaises(SystemExit):
            cua.parse_point("12", "--from")

    def test_rejects_non_numeric(self):
        with self.assertRaises(SystemExit):
            cua.parse_point("a,b", "--to")


class SessionSecurityTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("CODEX_CUA_HOME")
        os.environ["CODEX_CUA_HOME"] = self.home.name

    def tearDown(self):
        if self.previous_home is None:
            os.environ.pop("CODEX_CUA_HOME", None)
        else:
            os.environ["CODEX_CUA_HOME"] = self.previous_home
        self.home.cleanup()

    def test_session_uses_random_private_endpoint_and_metadata(self):
        first, first_listener = cua._create_session()
        second, second_listener = cua._create_session()
        try:
            self.assertNotEqual(first.socket_path, second.socket_path)
            for path, mode in (
                (Path(self.home.name), 0o700),
                (first.session_dir, 0o700),
                (first.auth_path, 0o600),
                (first.endpoint, 0o600),
                (first.socket_path, 0o600),
            ):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode)
            endpoint = json.loads(first.endpoint.read_text())
            self.assertNotIn("token", endpoint)
            self.assertTrue(endpoint["socket"].startswith(str(Path(self.home.name))))
        finally:
            cua._cleanup_session(first, first_listener)
            cua._cleanup_session(second, second_listener)

    def test_pid_log_and_lock_files_are_private(self):
        session, listener = cua._create_session()
        lock_fd = cua._open_lock()
        log_fd = cua._private_file_fd(
            cua.log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, repair_mode=True
        )
        os.close(log_fd)
        try:
            for path in (cua.pid_path(), cua.log_path(), cua.lock_path()):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        finally:
            os.close(lock_fd)
            cua._cleanup_session(session, listener)

    def test_challenge_response_accepts_signed_request(self):
        session, listener = cua._create_session()
        received = []

        def serve():
            listener.settimeout(5)
            conn, _ = listener.accept()
            with conn:
                received.append(cua._server_handshake(conn, session))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(str(session.socket_path))
                cua._client_handshake(conn, session, {"op": "ping"})
            thread.join(timeout=2)
            self.assertEqual(received, [{"op": "ping"}])
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():
                listener.close()
                thread.join(timeout=2)
            cua._cleanup_session(session, listener)

    def test_wrong_mac_is_rejected_before_command_dispatch(self):
        session, listener = cua._create_session()
        result = []

        class FakeServer:
            thread_id = "test"

            def call_tool(self, *_args, **_kwargs):
                result.append("dispatched")
                return {}

        def serve():
            listener.settimeout(5)
            conn, _ = listener.accept()
            with conn:
                result.append(
                    cua._handle_daemon_connection(conn, session, FakeServer(), set())
                )

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(str(session.socket_path))
                cua._recv_frame(conn)
                cua._send_frame(conn, {"payload": {"op": "ping"}, "mac": "0" * 64})
                self.assertEqual(cua._recv_frame(conn), {"ok": False, "error": "unauthorized"})
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [False])
        finally:
            if thread.is_alive():
                listener.close()
                thread.join(timeout=2)
            cua._cleanup_session(session, listener)

    def test_large_reply_uses_reply_frame_limit(self):
        session, listener = cua._create_session()
        result = []
        large_text = "x" * (cua.IPC_MAX_FRAME + 4096)

        class FakeServer:
            thread_id = "test"

            def call_tool(self, *_args, **_kwargs):
                return {"content": [{"type": "text", "text": large_text}]}

        def serve():
            listener.settimeout(5)
            conn, _ = listener.accept()
            with conn:
                result.append(cua._handle_daemon_connection(conn, session, FakeServer(), set()))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(str(session.socket_path))
                cua._client_handshake(conn, session, {"tool": "list_apps", "arguments": {}})
                reply = cua._recv_frame(conn, limit=cua.IPC_MAX_REPLY_FRAME)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(result and result[0] is False)
            self.assertTrue(reply["ok"])
            self.assertGreater(len(reply["result"]["content"][0]["text"]), cua.IPC_MAX_FRAME)
        finally:
            if thread.is_alive():
                listener.close()
                thread.join(timeout=2)
            cua._cleanup_session(session, listener)

    def test_oversized_reply_is_reported_without_killing_handler(self):
        session, listener = cua._create_session()
        result = []
        previous_limit = cua.IPC_MAX_REPLY_FRAME
        cua.IPC_MAX_REPLY_FRAME = 1024

        class FakeServer:
            thread_id = "test"

            def call_tool(self, *_args, **_kwargs):
                return {"content": [{"type": "text", "text": "x" * 2048}]}

        def serve():
            listener.settimeout(5)
            conn, _ = listener.accept()
            with conn:
                result.append(cua._handle_daemon_connection(conn, session, FakeServer(), set()))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(str(session.socket_path))
                cua._client_handshake(conn, session, {"tool": "list_apps", "arguments": {}})
                reply = cua._recv_frame(conn)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [False])
            self.assertFalse(reply["ok"])
            self.assertIn("frame is too large", reply["error"])
        finally:
            cua.IPC_MAX_REPLY_FRAME = previous_limit
            if thread.is_alive():
                listener.close()
                thread.join(timeout=2)
            cua._cleanup_session(session, listener)

    def test_client_rejects_a_socket_without_server_proof(self):
        session, listener = cua._create_session()
        result = []

        def fake_server():
            listener.settimeout(5)
            conn, _ = listener.accept()
            with conn:
                conn.sendall(
                    (
                        json.dumps(
                            {
                                "version": cua.IPC_VERSION,
                                "challenge": "00" * 32,
                                "proof": "0" * 64,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                try:
                    result.append(cua._recv_frame(conn))
                except Exception as exc:  # noqa: BLE001 - assert no request was sent
                    result.append(exc)

        thread = threading.Thread(target=fake_server, daemon=True)
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(str(session.socket_path))
                with self.assertRaises(cua.PeerAuthenticationError):
                    cua._client_handshake(conn, session, {"op": "ping"})
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(result[0], cua.IPCError)
        finally:
            if thread.is_alive():
                listener.close()
                thread.join(timeout=2)
            cua._cleanup_session(session, listener)

    def test_peer_authorization_rejects_foreign_uid_and_team(self):
        with self.assertRaises(cua.PeerAuthenticationError):
            cua.authorize_peer(cua.PeerIdentity(os.getuid() + 1, None, None, None, None, None))
        previous = os.environ.get("CODEX_CUA_ALLOWED_TEAM_IDS")
        os.environ["CODEX_CUA_ALLOWED_TEAM_IDS"] = "trusted.team"
        try:
            with self.assertRaises(cua.PeerAuthenticationError):
                cua.authorize_peer(cua.PeerIdentity(os.getuid(), 1, os.getuid(), 1, "other.team", "id"))
        finally:
            if previous is None:
                os.environ.pop("CODEX_CUA_ALLOWED_TEAM_IDS", None)
            else:
                os.environ["CODEX_CUA_ALLOWED_TEAM_IDS"] = previous

    def test_request_validation_rejects_unknown_tools_and_fields(self):
        with self.assertRaises(cua.IPCError):
            cua._validate_request({"tool": "exec", "arguments": {}})
        with self.assertRaises(cua.IPCError):
            cua._validate_request({"tool": "list_apps", "arguments": {}, "extra": True})
        with self.assertRaises(cua.IPCError):
            cua._validate_request({"op": "ping", "token": "leak"})
        with self.assertRaises(cua.IPCError):
            cua._validate_request({"tool": "list_apps", "arguments": {}, "prime": "yes"})
        with self.assertRaises(cua.IPCError):
            cua._validate_request(
                {
                    "tool": "get_app_state",
                    "arguments": {"app": "Notes"},
                    "match": {"pattern": "["},
                }
            )

    def test_daemon_alive_treats_invalid_endpoint_as_dead(self):
        endpoint = cua.endpoint_path()
        cua._write_private(endpoint, b"{}\n")
        try:
            self.assertFalse(cua.daemon_alive())
        finally:
            endpoint.unlink(missing_ok=True)

    def test_endpoint_symlink_and_unsafe_directory_are_rejected(self):
        session, listener = cua._create_session()
        try:
            # Replacing the auth path with a symlink must not make the client
            # read a file outside the session directory.
            session.auth_path.unlink()
            outside = Path(self.home.name) / "outside-token"
            outside.write_text(session.token.hex())
            session.auth_path.symlink_to(outside)
            with self.assertRaises(cua.IPCError):
                cua._load_session()
        finally:
            session.auth_path.unlink(missing_ok=True)
            cua._cleanup_session(session, listener)

    def test_custom_broad_directory_is_not_chmodded(self):
        broad = Path(self.home.name) / "shared"
        broad.mkdir(mode=0o755)
        previous = os.environ["CODEX_CUA_HOME"]
        os.environ["CODEX_CUA_HOME"] = str(broad)
        try:
            with self.assertRaises(cua.IPCError):
                cua.state_dir()
            self.assertEqual(stat.S_IMODE(os.stat(broad).st_mode), 0o755)
        finally:
            os.environ["CODEX_CUA_HOME"] = previous

    def test_directory_named_codex_cua_is_not_treated_as_default(self):
        broad = Path(self.home.name) / "codex-cua"
        broad.mkdir(mode=0o755)
        previous = os.environ["CODEX_CUA_HOME"]
        os.environ["CODEX_CUA_HOME"] = str(broad)
        try:
            with self.assertRaises(cua.IPCError):
                cua.state_dir()
            self.assertEqual(stat.S_IMODE(os.stat(broad).st_mode), 0o755)
        finally:
            os.environ["CODEX_CUA_HOME"] = previous

    def test_relative_home_is_normalized_for_endpoint_paths(self):
        previous = os.environ["CODEX_CUA_HOME"]
        previous_cwd = os.getcwd()
        with tempfile.TemporaryDirectory(dir="/tmp") as holder:
            os.chdir(holder)
            os.environ["CODEX_CUA_HOME"] = "relative-state"
            try:
                session, listener = cua._create_session()
                try:
                    self.assertTrue(session.endpoint.is_absolute())
                    self.assertEqual(
                        session.endpoint,
                        Path(os.path.abspath("relative-state/session.json")),
                    )
                    self.assertEqual(cua._load_session(), session)
                finally:
                    cua._cleanup_session(session, listener)
            finally:
                os.environ["CODEX_CUA_HOME"] = previous
                os.chdir(previous_cwd)

    def test_missing_state_parents_are_created_private(self):
        previous = os.environ["CODEX_CUA_HOME"]
        with tempfile.TemporaryDirectory(dir="/tmp") as holder:
            root = Path(holder) / "one" / "two"
            os.environ["CODEX_CUA_HOME"] = str(root)
            try:
                self.assertEqual(cua.state_dir(), Path(os.path.abspath(root)))
                self.assertEqual(stat.S_IMODE(os.stat(root / ".." / "..").st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(root / "..").st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)
            finally:
                os.environ["CODEX_CUA_HOME"] = previous

    def test_expected_pid_is_required_for_client_peer(self):
        identity = cua.PeerIdentity(os.getuid(), os.getpid(), os.getuid(), os.getpid(), None, None)
        with self.assertRaises(cua.PeerAuthenticationError):
            cua.authorize_peer(identity, expected_pid=os.getpid() + 1)

    def test_darwin_peer_token_option_is_kernel_audit_token(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin local-socket options are not available")
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            raw = cua._sockopt_bytes(left, 0x006, 32)
            self.assertEqual(len(raw or b""), 32)
            identity = cua.peer_identity(left)
            self.assertEqual(identity.audit_pid, os.getpid())
            self.assertEqual(identity.audit_uid, os.getuid())
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
