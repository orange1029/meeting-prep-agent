"""
deploy_config.py — 部署配置层

这个模块解决一件事：**在客户电脑上跑起来需要哪些配置，这些配置从哪来。**

为什么要单独抽出来？
  交付给客户时，最常见的失败不是代码崩了，而是「配置不对但没人知道哪不对」。
  FDE 现场最耗时间的往往是一句话：「这个参数到底该配在哪」。

所以这里定了三条规矩：

  1. **配置外置** —— 密钥、模型名、端点、输出目录全在外部文件里，
     不烤进代码、不烤进 exe。客户改配置不用重新打包。
  2. **优先级明确** —— 命令行 > 环境变量 > 配置文件 > 默认值。
     每一条都能说清楚「我现在用的是哪来的」。
  3. **能自检** —— 提供体检命令，一次性告诉客户「哪里没配好」，
     而不是让他跑一次才知道报什么错。

配置来源的查找顺序（先找到的赢）：
  ① 命令行参数 --config 指定的文件
  ② 环境变量 DEPLOY_CONFIG 指定的文件
  ③ 程序所在目录的 config.toml
  ④ 当前工作目录的 config.toml
  ⑤ 环境变量（DEEPSEEK_API_KEY 等）
  ⑥ 内置默认值

★ 关键设计：exe 打包后，「程序所在目录」不等于「当前工作目录」。
  客户双击 exe 时工作目录可能是任意路径，所以路径判断要两种都试。
"""

import os
import sys

# ============ Windows 中文控制台编码处理 ============
# ★ 客户 90% 用中文 Windows，控制台默认 GBK，
#   打印 ✓ ✠ 这类符号会抛 UnicodeEncodeError。
#   必须在任何输出之前修，且要同时修 stdout 和 stderr。
def _修控制台编码():
    for 流 in (sys.stdout, sys.stderr):
        if 流 is None:
            continue
        try:
            流.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


_修控制台编码()

# ============ 配置文件格式：TOML（Python 3.11+ 内置 tomllib，无需装包）============
# 为什么用 TOML 而不是 JSON/YAML：
#   · JSON 不支持注释，客户看不懂
#   · YAML 要装 pyyaml
#   · TOML 是标准，注释友好，且 Python 3.11+ 自带 tomllib
#   （3.10 及以下会 fallback 到极简解析器，代码里有兼容处理）

# ============ 各项配置的默认值 ============
# 没有任何一项包含密钥。密钥永远来自环境变量或配置文件，
# 不进默认值、不进代码、不进 git。

DEFAULTS = {
    "api_key": "",              # 大模型 API 密钥（必填）
    "base_url": "https://api.deepseek.com",  # 接口地址
    "model": "deepseek-v4-flash",           # 模型名
    "output_dir": "briefings",              # 简报输出目录
    "memory_dir": "",                       # 记忆库目录（空=用程序同级的 memory/）
    "timeout": 60,                          # 单次请求超时（秒）
    "max_rounds": 6,                        # Agent 模式最大轮数
    "kill_switch_file": "kill_switch.json", # 紧急停止开关文件
}


def _程序目录():
    """程序所在目录。

    打包成 exe 后 sys.executable 是 exe 本身的路径，
    源码运行时它是 python 解释器路径（在同一目录下）。
    两种情况都要能定位到「程序旁边」的文件。
    """
    if getattr(sys, "frozen", False):
        # 打包成 exe：取 exe 所在目录
        return os.path.dirname(os.path.abspath(sys.executable))
    # 源码运行：取脚本所在目录
    return os.path.dirname(os.path.abspath(__file__))


def _读toml(路径):
    """读 TOML 文件。优先用标准库 tomllib，失败则用极简解析器兜底。

    极简解析器只支持最常见的写法：
        键 = "值"
        键 = 数字
    够客户用了，而且不引第三方依赖。
    """
    if not os.path.exists(路径):
        return {}
    with open(路径, "r", encoding="utf-8") as f:
        内容 = f.read()

    try:
        import tomllib
        return tomllib.loads(内容)
    except ImportError:
        pass
    except Exception as e:
        print(f"[配置] {os.path.basename(路径)} 解析失败：{e}")
        print("[配置] 请检查格式（每行应为 键 = 值）")
        return {}

    # Python 3.10 及以下：没有 tomllib，用极简解析器
    结果 = {}
    for 行 in 内容.splitlines():
        行 = 行.strip()
        if not 行 or 行.startswith("#") or "=" not in 行:
            continue
        键, _, 值 = 行.partition("=")
        键 = 键.strip()
        值 = 值.strip()
        # 去掉引号
        if len(值) >= 2 and 值[0] == 值[-1] and 值[0] in "\"'":
            值 = 值[1:-1]
        # 转数字
        try:
            if "." in 值:
                值 = float(值)
            else:
                值 = int(值)
        except ValueError:
            pass
        结果[键] = 值
    return 结果


def 找配置文件(指定路径=None):
    """按优先级找到要用的配置文件。返回路径或 None。"""
    候选 = []

    if 指定路径:
        候选.append(("命令行指定", 指定路径))
    if os.environ.get("DEPLOY_CONFIG"):
        候选.append(("环境变量 DEPLOY_CONFIG", os.environ["DEPLOY_CONFIG"]))

    程序目录 = _程序目录()
    候选.append(("程序目录", os.path.join(程序目录, "config.toml")))
    候选.append(("当前目录", os.path.join(os.getcwd(), "config.toml")))

    for 来源, 路径 in 候选:
        if os.path.exists(路径):
            return 路径, 来源
    return None, None


def 读配置(指定路径=None):
    """读全部配置，返回 (配置字典, 配置文件路径, 来源说明)。

    ★ 每次调用都重新读文件，不缓存。
    原因：客户改了 config.toml 或 kill_switch.json 后要立即生效，
    不能要求重启程序。FDE 陪跑项目里踩过这个坑——
    kill switch 必须状态外置且每次读文件。

    ★ 优先级（从高到低）：
        ① 命令行 --config 指定的文件里**明确写了**的项
        ② 环境变量 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
        ③ 自动找到的配置文件里明确写了���项
        ④ 内置默认值

    【为什么这么排】
    · 环境变量排在「命令行指定文件」之后，但在「自动找到的文件」之前。
      理由：客户显式指定了配置文件，那个文件里的值是他明确写的，
      不该被环境变量悄悄盖掉（否则客户改了文件发现没生效，会很困惑）；
      但如果那个文件**没写**某一项，环境变量应该能兜底
      ——否则运维临时设个环境变量想覆盖模型名，在 `--config` 模式下就不生效了。
    · 自动找到的配置文件排在环境变量之后：
      客户可能没意识到有 config.toml（比如从别处拷来的旧文件），
      这时运维临时设个环境变量就能覆盖，不用去改文件。
    """
    路径, 来源 = 找配置文件(指定路径)
    文件配置 = _读toml(路径) if 路径 else {}

    # 文件里的段（[llm] / [paths] / [safety]）拍平成带前缀的键
    扁平 = {}
    for 段名, 段内容 in 文件配置.items():
        if isinstance(段内容, dict):
            for 键, 值 in 段内容.items():
                扁平[f"{段名}.{键}"] = 值
        else:
            扁平[段名] = 段内容

    配置 = dict(DEFAULTS)
    配置["_config_path"] = 路径 or ""
    配置["_config_source"] = 来源 or "（未找到配置文件，全部用默认值）"

    # ---- 第 ① 步：环境变量先兜底（作为「文件没写」时的默认）----
    环境映射 = {
        "DEEPSEEK_API_KEY": "api_key",
        "DEEPSEEK_BASE_URL": "base_url",
        "DEEPSEEK_MODEL": "model",
    }
    for 环境名, 配置键 in 环境映射.items():
        值 = os.environ.get(环境名, "").strip()
        if 值:
            配置[配置键] = 值

    # ---- 第 ② 步：文件里明确写了什么 ----
    # 分两种情况：
    #   · 显式指定（--config）→ 文件赢。客户明确写的，不该被环境变量盖掉。
    #   · 自动找到 → 环境变量赢。客户可能没意识到这个文件存在
    #     （比如从别处拷来的旧配置），运维设个环境变量就该能临时覆盖。
    for 键, 值 in 扁平.items():
        短键 = 键.split(".")[-1]
        if 短键 not in 配置:
            continue
        if 指定路径 or 短键 not in ("api_key", "base_url", "model"):
            # 显式指定 → 文件赢
            # 自动找到 → 只有非环境变量类配置项（output_dir 等）才让文件赢
            配置[短键] = 值

    return 配置


def 存配置(配置, 路径=None):
    """把配置写回文件（供首次部署用）。"""
    路径 = 路径 or os.path.join(_程序目录(), "config.toml")
    内容 = f"""# 会前简报 Agent —— 部署配置
# 改完这个文件立即生效，不需要重启程序。

[llm]
# 大模型接口地址
base_url = "{配置.get('base_url', '')}"
# 模型名
model = "{配置.get('model', '')}"
# 密钥建议用环境变量，不要写在这里。
# 写在这里也能用，但这个文件别外传。

[paths]
# 简报输出目录（相对路径以程序目录为基准）
output_dir = "{配置.get('output_dir', 'briefings')}"
# 记忆库目录（留空=用程序同级的 memory/）
memory_dir = "{配置.get('memory_dir', '')}"

[safety]
# 单次请求超时（秒）
timeout = {配置.get('timeout', 60)}
# Agent 模式最大轮数
max_rounds = {配置.get('max_rounds', 6)}
"""
    with open(路径, "w", encoding="utf-8") as f:
        f.write(内容)
    return 路径


def 检查(配置):
    """体检。返回 (问题列表, 信息列表)。"""
    问题 = []
    信息 = []

    信息.append(f"配置文件：{配置.get('_config_source')}")
    if 配置.get("_config_path"):
        信息.append(f"  → {配置['_config_path']}")

    if not 配置.get("api_key"):
        问题.append(
            "缺少大模型 API 密钥。两种配法（任选其一）：\n"
            "    ① 设环境变量 DEEPSEEK_API_KEY=sk-xxx\n"
            "    ② 在 config.toml 的 [llm] 段加 api_key = \"sk-xxx\""
        )
    else:
        密钥 = 配置["api_key"]
        信息.append(f"API 密钥：已配置（{密钥[:6]}…{密钥[-4:]}，长度 {len(密钥)}）")
        if 密钥.startswith("sk-在这里"):
            问题.append("API 密钥还是 .env.example 里的占位符，没换成真密钥")

    信息.append(f"接口地址：{配置.get('base_url')}")
    信息.append(f"模型    ：{配置.get('model')}")

    输出目录 = 配置.get("output_dir", "briefings")
    if os.path.isabs(输出目录):
        实际目录 = 输出目录
    else:
        实际目录 = os.path.join(_程序目录(), 输出目录)
    try:
        os.makedirs(实际目录, exist_ok=True)
        信息.append(f"输出目录：{实际目录} ✓")
    except Exception as e:
        问题.append(f"输出目录不可写：{实际目录}（{e}）")

    记忆目录 = 配置.get("memory_dir") or os.path.join(_程序目录(), "memory")
    if os.path.exists(记忆目录):
        记忆文件 = os.path.join(记忆目录, "memories.json")
        if os.path.exists(记忆文件):
            信息.append(f"记忆库  ：{记忆文件} ✓")
        else:
            信息.append(f"记忆库  ：{记忆目录}（还没有 memories.json，属首次部署）")
    else:
        信息.append(f"记忆库  ：{记忆目录}（目录不存在，属首次部署，程序会自动创建）")

    return 问题, 信息


if __name__ == "__main__":
    # 单独运行这个文件 = 体检
    import json

    配置 = 读配置()
    问题, 信息 = 检查(配置)

    print("=" * 60)
    print("  部署体检")
    print("=" * 60)
    for 行 in 信息:
        print(f"  {行}")

    if 问题:
        print("\n【发现问题】")
        for 行 in 问题:
            print(f"  ✗ {行}")
        sys.exit(1)
    else:
        print("\n  ✓ 一切正常，可以运行")
