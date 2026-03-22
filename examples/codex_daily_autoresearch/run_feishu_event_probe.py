from __future__ import annotations

import json
import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from feishu_bot import _require_lark_oapi, _resolve_feishu_domain


def main() -> None:
    app_id = str(os.environ.get("QLIB_FEISHU_APP_ID") or os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.environ.get("QLIB_FEISHU_APP_SECRET") or os.environ.get("FEISHU_APP_SECRET") or "").strip()
    domain = str(os.environ.get("QLIB_FEISHU_DOMAIN") or os.environ.get("FEISHU_DOMAIN") or "feishu").strip()

    if not app_id or not app_secret:
        raise SystemExit("Set QLIB_FEISHU_APP_ID and QLIB_FEISHU_APP_SECRET before starting the probe.")

    sdk = _require_lark_oapi()

    def handle_message_event(data) -> None:
        event = getattr(data, "event", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        message = getattr(event, "message", None)
        payload = {
            "event_type": "im.message.receive_v1",
            "open_id": getattr(sender_id, "open_id", None),
            "user_id": getattr(sender_id, "user_id", None),
            "union_id": getattr(sender_id, "union_id", None),
            "chat_id": getattr(message, "chat_id", None),
            "chat_type": getattr(message, "chat_type", None),
            "message_id": getattr(message, "message_id", None),
            "message_type": getattr(message, "message_type", None),
            "content": getattr(message, "content", None),
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    dispatcher = sdk.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle_message_event).build()
    client = sdk.ws.Client(
        app_id,
        app_secret,
        log_level=sdk.LogLevel.INFO,
        event_handler=dispatcher,
        domain=_resolve_feishu_domain(sdk, domain),
    )
    print("Feishu event probe started. Send a DM and a group @mention to the bot.", flush=True)
    client.start()


if __name__ == "__main__":
    main()
