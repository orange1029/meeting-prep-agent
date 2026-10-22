"""
test_deploy.py — 交付工程层的测试

为什么要单独测这一层？
  交付层（配置、kill switch、打包）**不测就是定时炸弹**：
  平时开发在本机跑，一切正常；
  一旦拿到客户机器上才发现「配置读不到」「开关拦错了」「输出存到奇怪的地方」，
  现场改代码重新打包是来不及的。

  而且这一层特别容易出「看起来对其实错」的 bug——
  比如 kill switch 默认停用，会导致客户部署完直接跑不起来，
  而这在开发机上根本看不出来（因为开发机的文件状态不一样）。

本文件**零 API 调用、零外部依赖**（除了 tomllib，Python 3.11+ 自带）。
"""

import json
import os
import shutil
import sys
import tempfile
from unittest import mock

# 把项目根目录加进 import 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deploy_config
import kill_switch

RESULTS = {"pass": 0, "fail": 0}


def check(label, cond, detail=""):
    ok = bool(cond)
    RESULTS["pass" if ok else "fail"] += 1
    print(f"  {'✓' if ok else '✗'} {label}"
          + (f"  → {detail}" if not ok and detail else ""))
    return ok


# ============ A 组：配置读取 ============

def test_config_read():
    print("\n【A】配置读取")

    临时 = tempfile.mkdtemp(prefix="deploycfg_")
    try:
        # --- 配置文件优先于默认值 ---
        路径 = os.path.join(临时, "config.toml")
        with open(路径, "w", encoding="utf-8") as f:
            f.write('[llm]\nmodel = "my-model"\nbase_url = "http://x"\n'
                    '[paths]\noutput_dir = "out"\n'
                    '[safety]\ntimeout = 99\n')

        配置 = deploy_config.读配置(路径)
        check("读到配置文件里的 model", 配置["model"] == "my-model", 配置["model"])
        check("读到 base_url", 配置["base_url"] == "http://x")
        check("读到 output_dir", 配置["output_dir"] == "out")
        check("读到 timeout", 配置["timeout"] == 99)
        check("没配的项走默认值", 配置["max_rounds"] == 6)
        check("记录了配置来源", "命令行" in 配置["_config_source"], 配置["_config_source"])

        # --- 环境变量 vs 显式指定的文件 ---
        # 显式指定的文件里写了什么就以文件为准（客户的明确意图）
        with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "env-model"}):
            配置2 = deploy_config.读配置(路径)
            check("显式指定文件 > 环境变量",
                  配置2["model"] == "my-model", 配置2["model"])

        # --- 但文件没写的项，环境变量要能兜底 ---
        # 否则 --config 模式下运维设环境变量想临时改模型名就不生效了
        缺项 = os.path.join(临时, "partial.toml")
        with open(缺项, "w", encoding="utf-8") as f:
            f.write('[llm]\nbase_url = "http://only-this"\n')  # 没写 model
        with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "env-fallback"}):
            配置2b = deploy_config.读配置(缺项)
            check("文件没写的项用环境变量兜底",
                  配置2b["model"] == "env-fallback", 配置2b["model"])
            check("文件写了的项仍然生效",
                  配置2b["base_url"] == "http://only-this", 配置2b["base_url"])

        # --- 配置文件不存在时用默认值 ---
        配置3 = deploy_config.读配置(os.path.join(临时, "不存在.toml"))
        check("文件不存在用默认值", 配置3["model"] == "deepseek-v4-flash")
        check("文件不存在不报错", 配置3["_config_path"] == "")

        # --- 坏 TOML 不崩 ---
        坏路径 = os.path.join(临时, "bad.toml")
        with open(坏路径, "w", encoding="utf-8") as f:
            f.write("这不是 TOML [[[\n")
        try:
            配置4 = deploy_config.读配置(坏路径)
            check("坏配置不崩", isinstance(配置4, dict))
        except Exception as e:
            check("坏配置不崩", False, f"{type(e).__name__}: {e}")

    finally:
        shutil.rmtree(临时, ignore_errors=True)


def test_config_precedence():
    print("\n【B】优先级：显式指定文件 > 环境变量 > 自动找到的文件 > 默认值")

    临时 = tempfile.mkdtemp(prefix="deployprio_")
    try:
        路径 = os.path.join(临时, "config.toml")
        with open(路径, "w", encoding="utf-8") as f:
            f.write('[llm]\nmodel = "from-file"\n')

        # 只有配置文件
        配置 = deploy_config.读配置(路径)
        check("配置文件生效", 配置["model"] == "from-file")

        # 加环境变量 → 这里传的是显式路径，所以文件赢（客户的明确意图）
        with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "from-env"}):
            配置 = deploy_config.读配置(路径)
            check("显式指定文件 > 环境变量",
                  配置["model"] == "from-file", 配置["model"])

        # 命令行指定了另一个文件 → 那个文件赢（哪怕有环境变量）
        另一个 = os.path.join(临时, "other.toml")
        with open(另一个, "w", encoding="utf-8") as f:
            f.write('[llm]\nmodel = "from-other-file"\n')
        with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "from-env"}):
            配置 = deploy_config.读配置(另一个)
            check("命令行文件 > 环境变量",
                  配置["model"] == "from-other-file", 配置["model"])

        # ★ 但「自动找到的配置文件」< 环境变量
        # 理由：客户可能没意识到有 config.toml（比如从别处拷来的），
        # 这时运维临时设个环境变量就能覆盖，不用去改文件。
        with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "from-env"}):
            with mock.patch.object(deploy_config, "找配置文件",
                                   return_value=(路径, "程序目录")):
                配置 = deploy_config.读配置(None)
                check("自动找到的配置文件 < 环境变量",
                      配置["model"] == "from-env", 配置["model"])
    finally:
        shutil.rmtree(临时, ignore_errors=True)


def test_config_check():
    print("\n【C】体检")

    # 没密钥 → 报问题
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch.object(deploy_config, "读配置", return_value=dict(
                deploy_config.DEFAULTS, _config_source="测试")):
            问题, 信息 = deploy_config.检查(dict(
                deploy_config.DEFAULTS, _config_source="测试"))
            check("缺密钥时报问题", any("密钥" in p for p in 问题))
            check("给出了两种配法",
                  any("环境变量" in p and "config.toml" in p for p in 问题))

    # 占位符密钥 → 也报问题
    占位 = dict(deploy_config.DEFAULTS,
                api_key="sk-在这里粘贴你的密钥", _config_source="测试")
    问题, _ = deploy_config.检查(占位)
    check("占位符密钥被识别", any("占位符" in p for p in 问题))

    # 正常配置 → 无问题
    正常 = dict(deploy_config.DEFAULTS,
                api_key="sk-1234567890abcdef", _config_source="测试")
    问题, 信息 = deploy_config.检查(正常)
    check("正常配置无问题", not 问题, str(问题))
    # 密钥只显示头尾（不能全打出来——体检输出可能被贴到工单或聊天里）
    密钥行 = [i for i in 信息 if "密钥" in i]
    check("有密钥信息行", 密钥行, str(信息))
    if 密钥行:
        行 = 密钥行[0]
        check("密钥没被完整显示", "sk-1234567890abcdef" not in 行, 行)
        check("显示了首尾片段", "…" in 行, 行)
        check("显示了长度", "19" in 行, 行)


# ============ D 组：kill switch ============

def test_kill_switch():
    print("\n【D】kill switch")

    临时 = tempfile.mkdtemp(prefix="killsw_")
    文件 = os.path.join(临时, "kill_switch.json")
    try:
        # --- 文件不存在 → 启用（人工触发工具不该被阻断）---
        启用, 说明 = kill_switch.读状态(文件)
        check("文件不存在 = 启用", 启用, 说明)
        check("说明里写清了依据", "启用" in 说明, 说明)

        # --- 停用 ---
        kill_switch.停用("客户要求暂停", 文件)
        启用, 说明 = kill_switch.读状态(文件)
        check("停用生效", not 启用, 说明)
        check("保留了停用原因", "客户要求暂停" in 说明, 说明)

        # --- 启用 ---
        kill_switch.启用("已确认", 文件)
        启用, _ = kill_switch.读状态(文件)
        check("启用生效", 启用)

        # --- 文件损坏 → 停用（不确定时不能瞎猜）---
        with open(文件, "w", encoding="utf-8") as f:
            f.write("{这不是合法 JSON")
        启用, 说明 = kill_switch.读状态(文件)
        check("文件损坏 = 停用", not 启用, 说明)
        check("说明里说了按停用处理", "停用" in 说明, 说明)

        # --- 文件内容不是对象 → 停用 ---
        with open(文件, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        启用, _ = kill_switch.读状态(文件)
        check("格式错误 = 停用", not 启用)

        # --- 写了 enabled 但没有其他字段 → 能正常读 ---
        with open(文件, "w", encoding="utf-8") as f:
            json.dump({"enabled": True}, f)
        启用, _ = kill_switch.读状态(文件)
        check("只有 enabled 字段也能读", 启用)
    finally:
        shutil.rmtree(临时, ignore_errors=True)


def test_kill_switch_每次重读():
    print("\n【E】kill switch 每次重读（不缓存）")

    临时 = tempfile.mkdtemp(prefix="killsw2_")
    文件 = os.path.join(临时, "kill_switch.json")
    try:
        kill_switch.启用("初始", 文件)
        启用1, _ = kill_switch.读状态(文件)
        check("第一次读：启用", 启用1)

        # 外部改了文件（模拟运维直接改）
        with open(文件, "w", encoding="utf-8") as f:
            json.dump({"enabled": False, "reason": "运维手工停用"}, f)

        启用2, 说明 = kill_switch.读状态(文件)
        check("外部改文件后立即生效（不缓存）", not 启用2, 说明)
        check("读到新的原因", "运维手工停用" in 说明, 说明)
    finally:
        shutil.rmtree(临时, ignore_errors=True)


# ============ F 组：主程序的部署行为 ============

def test_main_deploy_behavior():
    print("\n【F】主程序的部署行为")

    import meeting_prep_agent as mpa

    # --- 停用时拒绝执行，且退出码是 3 ---
    临时 = tempfile.mkdtemp(prefix="maindep_")
    开关 = os.path.join(临时, "kill_switch.json")
    try:
        kill_switch.停用("测试", 开关)
        with mock.patch.object(kill_switch, "读状态",
                               return_value=(False, "测试停用")):
            with mock.patch.object(sys, "argv", [
                    "meeting_prep_agent.py", "--client", "X",
                    "--industry", "Y", "--topic", "Z"]):
                try:
                    mpa.main()
                    check("停用时拒绝执行", False, "没抛异常")
                except SystemExit as e:
                    check("停用时拒绝执行", True)
                    check("退出码是 3（专用码）", e.code == 3, f"实际 {e.code}")
    finally:
        shutil.rmtree(临时, ignore_errors=True)

    # --- 参数缺失时给清晰提示 ---
    with mock.patch.object(kill_switch, "读状态", return_value=(True, "启用")):
        with mock.patch.object(sys, "argv", ["meeting_prep_agent.py"]):
            try:
                mpa.main()
                check("参数缺失时报错", False, "没抛异常")
            except SystemExit as e:
                check("参数缺失时报错", e.code == 1, f"退出码 {e.code}")


def test_output_dir_路径基准():
    print("\n【G】输出目录以程序目录为基准")

    # 客户双击 exe 时 cwd 是随机的，
    # 输出目录必须以程序目录为基准，不能用 cwd。
    临时 = tempfile.mkdtemp(prefix="outdir_")
    旧cwd = os.getcwd()
    try:
        os.chdir(临时)   # 模拟「双击 exe，cwd 是别的地方」
        配置 = dict(deploy_config.DEFAULTS,
                    output_dir="briefings", _config_source="测试")
        with mock.patch.object(deploy_config, "读配置", return_value=配置):
            类 = type("假参数", (), {"out": None, "client": "测试客户",
                                     "config": None})()
            路径 = mpa_save(类)
        check("输出落在程序目录而非 cwd",
              os.path.abspath(os.path.dirname(路径)) ==
              os.path.abspath(os.path.join(deploy_config._程序目录(), "briefings")),
              路径)
    finally:
        os.chdir(旧cwd)
        shutil.rmtree(临时, ignore_errors=True)


def mpa_save(参数):
    """调用 save() 并返回生成的路径。"""
    import meeting_prep_agent as mpa
    import io
    import contextlib
    缓冲 = io.StringIO()
    with contextlib.redirect_stdout(缓冲):
        mpa.save("# 测试简报\n", 参数)
    return os.path.join(deploy_config._程序目录(), "briefings",
                        f"测试客户_{__import__('datetime').date.today().isoformat()}.md")


def main():
    print("=" * 64)
    print("  交付工程层测试")
    print("=" * 64)

    test_config_read()
    test_config_precedence()
    test_config_check()
    test_kill_switch()
    test_kill_switch_每次重读()
    test_main_deploy_behavior()
    test_output_dir_路径基准()

    print("\n" + "=" * 64)
    总数 = RESULTS["pass"] + RESULTS["fail"]
    print(f"结果：{RESULTS['pass']}/{总数} 通过")
    if RESULTS["fail"]:
        print("✗ 有用例失败")
        sys.exit(1)
    print("✓ 交付层全部正常")


if __name__ == "__main__":
    main()
