"""Local product demo CLI: `app` serves the chat UI, demo sign-in and knowledge base."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import APIConfig, ChatCompletionsModel, load_dotenv
from .sessions import SessionError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _live_model():
    """Cached requests must not depend on today's API configuration.

    The server wraps this factory in models.LazyModel, so the project .env and the
    API configuration are read only when a request first needs the model. The
    names are looked up in this module, where the no-live-configuration guards
    patch them.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    return ChatCompletionsModel(APIConfig.from_env())
_live_model.mode = "live_agent"   # LazyModel.mode reads it before construction


def _runtime_options(parser):
    parser.add_argument("--run-timeout", type=float, default=120, help="每轮持久时间上限，默认120秒")
    parser.add_argument("--model-timeout", type=float, default=45, help="每次模型请求超时，另受剩余时间限制")
    parser.add_argument("--max-model-attempts", type=int, default=8, help="每轮模型尝试总上限，含重试，1至8次")
    parser.add_argument("--max-tool-calls", type=int, default=32, help="每轮工具调用总上限，1至32次")


def _runtime_limits(args):
    from .runtime import RuntimeLimits
    return RuntimeLimits(run_timeout=args.run_timeout, model_timeout=args.model_timeout,
                         max_model_attempts=args.max_model_attempts, max_tool_calls=args.max_tool_calls)


def _release_policy_config(knowledge_store, *, channel="development"):
    """Pin the active knowledge release for one channel, labelled as unverified.

    The release is fully validated once, here; the shared snapshot is built from
    that validation, so the server's load is a cache hit (binding and bytes).
    """
    from .knowledge_release import resolve_active_release
    from .policy import PublicPolicyConfig, load_policy_knowledge
    release, validated = resolve_active_release(knowledge_store, channel=channel, include_bundles=True)
    metadata = {key: release[key] for key in ('release_id', 'manifest_sha256', 'generation', 'channel')}
    metadata['effective_dates'] = 'unverified_snapshot'
    metadata['review_status'] = ('human_attestations_recorded' if channel == 'reviewed' else 'development_candidate')
    config = PublicPolicyConfig(*(release[key] for key in ('corpus_path', 'structure_path', 'relations_path')),
                                knowledge_store=knowledge_store, knowledge_release=metadata)
    load_policy_knowledge(config, validated=validated)
    return config


DEFAULT_APP_KNOWLEDGE_STORE = PROJECT_ROOT / "var" / "knowledge-store"


def _app_command(args):
    """One command for the product demo: chat UI, demo sign-in and knowledge base."""
    from .demo_model import KeywordDemoModel
    from .service import create_server, init_credentials, load_credentials
    data = args.data_dir
    data.mkdir(parents=True, exist_ok=True)
    credentials_path = data / "credentials.json"
    if not credentials_path.exists():
        init_credentials(credentials_path)
    config = None
    notices = []
    if args.knowledge_store is not None and args.knowledge_store.exists():
        config = _release_policy_config(args.knowledge_store)
    else:
        notices.append("未找到官方政策知识发布，仅启用模拟订单业务；知识库管理不可用。")
    live = args.live
    server = create_server(db_path=data / "app.sqlite3", credentials=load_credentials(credentials_path),
                           port=args.port, model_factory=_live_model if live else KeywordDemoModel,
                           public_policy_config=config,
                           log_path=data / "requests.jsonl", demo_login=True,
                           kb_store=(data / "kb") if config is not None else None,
                           agent_options={"runtime_limits": _runtime_limits(args)})
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        print(json.dumps({"url": url, "knowledge_base": url + "/kb",
                          "mode": "live_agent（真实模型，会产生 API 费用）" if live else "offline_keyword_demo（离线关键词脚本，不调用 API）",
                          "data_dir": str(data.resolve()), "notices": notices,
                          "stop": "按 Ctrl+C 停止服务"}, ensure_ascii=False, indent=2), flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="客服 Agent 学习项目：所有订单与退货均为本地模拟。")
    commands = parser.add_subparsers(dest="command", required=True)
    app = commands.add_parser("app", help="启动产品演示：聊天界面与知识库管理（默认离线；--live 使用 .env 中的真实模型）")
    app.add_argument("--live", action="store_true", help="使用 .env 中的真实模型，会产生 API 费用")
    app.add_argument("--port", type=int, default=8765)
    app.add_argument("--knowledge-store", type=Path, default=DEFAULT_APP_KNOWLEDGE_STORE,
                     help="官方政策知识发布存储；不存在时只启用模拟订单业务")
    app.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "var" / "app",
                     help="演示数据目录：会话数据库、凭据、请求日志与知识库版本")
    app.add_argument("--open", action="store_true", help="启动后用默认浏览器打开页面")
    _runtime_options(app)
    args = parser.parse_args(argv)
    try:
        return _app_command(args)
    except SessionError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        # Config errors name missing fields, never their values.
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
