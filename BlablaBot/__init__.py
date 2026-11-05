"""BlablaBot：GsCore 的 NIKKE 数据查询插件。"""
import os

# 仅用于脱离 GsCore 的本地渲染测试，正常部署不要设置。
if os.environ.get("BLABLABOT_STANDALONE") != "1":
    from gsuid_core.sv import SL, Plugins

    if "BlablaBot" not in SL.plugins:
        Plugins(
            name="BlablaBot",
            force_prefix=["nk"],
            allow_empty_prefix=False,
        )

    from gsuid_core.data_store import get_res_path

    # 首次启动：从旧目录 data/AliceUID 平滑迁移（用户无感）
    try:
        import shutil

        _res = get_res_path()
        _old_dir = _res / "AliceUID"
        _new_dir = _res / "BlablaBot"
        if _old_dir.exists() and not _new_dir.exists():
            shutil.copytree(_old_dir, _new_dir)
    except Exception:
        pass

    from .nikke_commands import configure
    configure(SL.plugins['BlablaBot'], get_res_path() / 'BlablaBot' / 'config.json')
    from .nikke_config.config import MAIN_PATH, AliceUIDConfig  # noqa: E402
    from .nikke_permissions.commands import setup as setup_group_permissions  # noqa: E402
    from .nikke_permissions.runtime import set_policy  # noqa: E402
    from .nikke_permissions.access import guard_plugin_triggers, set_group_observer  # noqa: E402

    policy = set_policy(setup_group_permissions(
        MAIN_PATH / "group_policy.json", config=AliceUIDConfig,
    ))

    # 先导入数据库模型，确保建表
    from .nikke_user import models as _models  # noqa: E402,F401
    from .nikke_user.admin import setup_admin  # noqa: E402
    try:
        setup_admin()
    except Exception as exc:
        from gsuid_core.logger import logger
        logger.warning(f'[BlablaBot] 数据库管理接入未完成，正常绑定功能保持可用 error_type={type(exc).__name__}')

    # 再导入命令与网页路由（导入即注册）
    from .nikke_login import commands as _commands  # noqa: E402,F401
    from .nikke_login import routes as _routes  # noqa: E402,F401
    from .nikke_profile import commands as _profile_commands  # noqa: E402,F401
    from .nikke_resource import commands as _resource_commands  # noqa: E402,F401
    from .nikke_help import commands as _help_commands  # noqa: E402,F401
    from .nikke_character import commands as _character_commands  # noqa: E402,F401
    from .nikke_export import commands as _export_commands  # noqa: E402,F401
    from .nikke_character import alias_commands as _alias_commands  # noqa: E402,F401
    from .nikke_uid import commands as _uid_commands  # noqa: E402,F401
    from .nikke_community.runtime import setup_community, observe_group  # noqa: E402
    from .nikke_calendar.commands import setup as setup_calendar  # noqa: E402
    from .nikke_database.commands import setup as setup_database_commands  # noqa: E402
    from .nikke_database.scheduler import setup as setup_database_scheduler  # noqa: E402
    from .nikke_roster.commands import setup as setup_roster  # noqa: E402
    from .nikke_union import commands as _union_commands  # noqa: E402
    from .nikke_solo import commands as _solo_commands  # noqa: E402,F401
    from .nikke_status import commands as _status_commands  # noqa: E402,F401
    # LPass 奖励中心：先导入模型建表，再注册 Lpass* 命令（prefix=False，不带 nk）
    from .nikke_lpass import store as _lpass_store  # noqa: E402,F401
    from .nikke_lpass import commands as _lpass_commands  # noqa: E402,F401
    from .nikke_lpass.scheduler import setup_auto as _setup_lpass_auto  # noqa: E402
    _setup_lpass_auto()

    def _database_config(key):
        return AliceUIDConfig.get_config(key).data

    async def _query_before_send(bot, ev):
        return await policy.query_allowed(getattr(ev, "group_id", None)) and policy.user_allowed(getattr(ev, "user_id", None))

    setup_calendar()
    from .nikke_raid_guide.commands import setup as setup_raid_guide
    setup_raid_guide(policy)
    setup_database_commands(_database_config)
    setup_database_scheduler(_database_config)
    setup_community(policy=policy)
    from .nikke_community.notify_policy import register as register_notify_commands
    register_notify_commands(AliceUIDConfig)
    from .nikke_harvest.commands import sv_harvest
    from .nikke_harvest.runtime import setup as setup_harvest, observe_route
    setup_harvest(sv_harvest)
    def observe_conversation(ev):
        observe_group(ev)
        observe_route(ev)
        from .nikke_cdk.runtime import observe_group as observe_cdk_group
        observe_cdk_group(ev)
    set_group_observer(observe_conversation)
    setup_roster(policy=policy)
    _union_commands.set_before_send(_query_before_send)
    from .nikke_cdk.runtime import setup as setup_cdk
    setup_cdk(policy)

    # 必须在全部 SV 和 setup 后扫描真实 Core Trigger，保留原生名单与 finally。
    guard_plugin_triggers(SL.plugins["BlablaBot"], policy)
