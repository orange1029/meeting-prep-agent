"""
kill_switch.py — 紧急停止开关

这是交付给客户时**不能省**的一件事。

为什么需要？
  这个工具会调大模型 API，会写简报文件，会读客户的历史沟通。
  万一在客户现场出了问题（数据不对、API 行为异常、客户不想要了），
  运维人员需要一个「立刻停下来」的手段，而不是：
    · 去找进程管理器杀进程（可能杀错别的）
    · 或者改代码重新打包（太慢）

★ 三条设计规矩（每条都有代价权衡，不是随便定的）：

  1. **状态外置** —— 开关状态存在独立文件里，不在内存。
     改了文件就生效，不需要重启程序。
     【为什么不能放内存】运维要停的是「正在跑的那个进程」，
     如果状态在内存里，就得先找到那个进程才能停——而 kill switch
     的意义就是「不用找到它也能停」。

  2. **每次读文件** —— 每次调用都重新读，不缓存。
     【为什么不能缓存】运维改完文件会以为已经生效，
     如果程序缓存了状态，改动要等重启才起作用——这在事故现场是要命的。
     代价是每次调用多一次文件读，对这个量级完全可接受。

  3. **默认状态要符合工具性质** —— 这里跟自动化系统不一样，要分情况：
     · **自动化系统**（定时跑、批量跑）→ 默认停用。文件丢了 = 停下，安全。
     · **人工触发的工具**（人点一下跑一次）→ 默认启用。文件丢了 = 继续跑，能用。
     本工具是后者：售前顾问主动触发，一次只处理一个客户。
     如果默认停用，运维每次部署完都得记得去开，
     而「部署完就忘了开」在现场是常态。
     → 所以：**文件不存在 = 启用**（不阻断）；
        **文件存在且 enabled=false = 停用**（明确停用）；
        **文件存在但损坏 = 停用**（读不出来时不能瞎猜，安全侧）。

     ⚠️ 这条和 FDE 陪跑项目里的「kill switch 默认偏安全」不矛盾——
     那条针对的是**自动扣款/自动发消息**这类无人值守场景。
     差别在于：有没有人在旁边看着。**看着的场景默认启用，没人看着的默认停用。**
"""

import json
import os
import sys

# ============ Windows 中文控制台编码处理 ============
# ★ 客户中文 Windows 控制台默认 GBK，
#   打印 ▶ ⏸ ✓ 这类符号会抛 UnicodeEncodeError。
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

# 开关文件的名字。放在程序旁边，方便运维找。
# 客户可以改名（配置项 kill_switch_file），但默认这个最好记。
默认文件名 = "kill_switch.json"


def _程序目录():
    """程序所在目录。打包成 exe 后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def 开关路径(配置文件名=None):
    """开关文件的绝对路径。"""
    名 = 配置文件名 or 默认文件名
    if os.path.isabs(名):
        return 名
    return os.path.join(_程序目录(), 名)


def 读状态(配置文件名=None):
    """读当前开关状态。返回 (是否启用, 状态说明)。

    ★ 任何异常都按「停用」处理——除了「文件不存在」这种情况。
    理由见文件头的第 3 条规矩：
      · 文件不存在     → 启用（人工触发工具，部署后不该被阻断）
      · 文件存在但读不出 → 停用（不确定时不能瞎猜）
      · 文件存在但 enabled=false → 停用（明确停用）
      · 文件存在且 enabled=true  → 启用
    """
    路径 = 开关路径(配置文件名)
    if not os.path.exists(路径):
        return True, f"未设置开关文件（{os.path.basename(路径)}），按「启用」处理"

    try:
        with open(路径, "r", encoding="utf-8") as f:
            数据 = json.load(f)
    except Exception as e:
        return False, f"开关文件读取失败（{e}），按「停用」处理"

    if not isinstance(数据, dict):
        return False, "开关文件格式错误（不是 JSON 对象），按「停用」处理"

    启用 = bool(数据.get("enabled", False))
    说明 = 数据.get("reason", "")
    状态 = 数据.get("updated_at", "")
    描述 = f"开关文件 {os.path.basename(路径)}：{'启用' if 启用 else '停用'}"
    if 说明:
        描述 += f"（原因：{说明}）"
    if 状态:
        描述 += f"（{状态}）"
    return 启用, 描述


def 停用(原因="手动停用", 配置文件名=None):
    """把开关置为停用。返回写入的路径。"""
    路径 = 开关路径(配置文件名)
    import datetime
    数据 = {
        "enabled": False,
        "reason": 原因,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "updated_by": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
    }
    with open(路径, "w", encoding="utf-8") as f:
        json.dump(数据, f, ensure_ascii=False, indent=2)
    return 路径


def 启用(原因="手动启用", 配置文件名=None):
    """把开关置为启用。返回写入的路径。

    ★ 恢复必须显式调这个函数（或写 enable.json），
    不能靠删文件——因为「文件不存在」在设计上是「停用」。
    """
    路径 = 开关路径(配置文件名)
    import datetime
    数据 = {
        "enabled": True,
        "reason": 原因,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "updated_by": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
    }
    with open(路径, "w", encoding="utf-8") as f:
        json.dump(数据, f, ensure_ascii=False, indent=2)
    return 路径


def 状态摘要(配置文件名=None):
    """给人看的一行状态。"""
    启用, 说明 = 读状态(配置文件名)
    return ("▶ 运行中" if 启用 else "⏸ 已停用") + " · " + 说明


if __name__ == "__main__":
    # 单独运行这个文件 = 查看/切换开关
    #   python kill_switch.py            查看当前状态
    #   python kill_switch.py off 原因    停用
    #   python kill_switch.py on  原因    启用
    参数 = sys.argv[1:]

    if not 参数:
        启用, 说明 = 读状态()
        print(状态摘要())
        sys.exit(0 if 启用 else 1)

    动作 = 参数[0].lower()
    原因 = " ".join(参数[1:]) if len(参数) > 1 else "手动操作"

    if 动作 in ("off", "stop", "disable", "停用"):
        路径 = 停用(原因)
        print(f"✓ 已停用。开关文件：{路径}")
        print("  正在运行的实例会在下一次检查时停下。")
        sys.exit(0)
    elif 动作 in ("on", "start", "enable", "启用"):
        路径 = 启用(原因)
        print(f"✓ 已启用。开关文件：{路径}")
        sys.exit(0)
    elif 动作 in ("status", "状态"):
        print(状态摘要())
        sys.exit(0)
    else:
        print("用法：")
        print("  python kill_switch.py                 查看状态")
        print("  python kill_switch.py off 原因         停用")
        print("  python kill_switch.py on  原因         启用")
        sys.exit(2)
