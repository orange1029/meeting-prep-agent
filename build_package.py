"""
build_package.py — 打交付包

产出三种形态，覆盖三类客户环境：

  ① dist-win/          单文件 exe（--onefile）
     客户什么都不用装。适合：客户电脑没 Python、没管理权限。
     代价：体积大（~15MB）、首次启动慢（解压到临时目录）、
           杀毒软件常误报（要提前跟客户 IT 打招呼）。

  ② dist-portable/    绿色包（源码 + 内置 venv + 启动脚本）
     客户解压双击即用。适合：客户允许装东西但不想折腾。
     代价：venv 不能跨机器拷贝，路径写死了 → 必须在客户机器上重建。

  ③ dist-docker/      Docker 镜像
     客户有 Docker 时最干净。适合：客户环境标准化、要求可审计。

★ 最重要的一个设计：不管哪种形态，
  **配置、密钥、记忆库、简报输出全在程序外面**，不烤进包里。
  理由：客户要改配置时不能重新打包；密钥不能跟着包走；
  客户的历史沟通数据不能躺在别人的交付物里。

用法：
    python build_package.py            # 全打
    python build_package.py exe        # 只打 exe
    python build_package.py portable   # 只打绿色包
    python build_package.py docker     # 只打 Docker
"""

import os
import shutil
import subprocess
import sys
import zipfile

# ============ 路径 ============
当前目录 = os.path.dirname(os.path.abspath(__file__))
输出根 = os.path.join(当前目录, "dist")
构建目录 = os.path.join(当前目录, ".build_tmp")

# ============ 打进包的核心文件 ============
# 客户真正需要的功能文件。演示文件（demo_autogen 等）不打——
# 交付物应该最小，攻击面越小越好。
核心文件 = [
    "meeting_prep_agent.py",    # 主程序
    "prep_tools_impl.py",       # 简报工具实现
    "prep_tools_schema.py",     # 简报工具定义
    "tools_impl.py",            # 业务数据层（mock 客户库等）
    "tools_schema.py",          # 通用工具定义
    "memory_store.py",          # 长期记忆库
    "memory_tools.py",          # 记忆工具
    "trace_logger.py",          # 链路追踪
    "deploy_config.py",         # 部署配置层
    "kill_switch.py",           # 紧急停止开关
    "requirements.txt",         # 依赖清单（setup 脚本要用）
]

# 不打进包但在包旁边放一份的（客户可能要看的文档）
随包文档 = [
    "README.md",
    "QUICKSTART.md",
]

# 明确排除的：绝不能进交付物
禁止文件 = [".env", ".github_token"]


def 打印(消息):
    print(f"[打包] {消息}", flush=True)


def 跑(命令, cwd=None, 描述=""):
    """跑一条命令。返回 (成功?, 输出)。"""
    打印(f"  $ {' '.join(命令[:3])} …" if 描述 else f"  $ {命令}")
    结果 = subprocess.run(命令, cwd=cwd, capture_output=True, text=True)
    if 结果.returncode != 0:
        打印(f"  ✗ 失败：{结果.stderr.strip()[:400]}")
        return False, 结果.stderr
    return True, 结果.stdout


def 准备目录():
    if os.path.exists(输出根):
        shutil.rmtree(输出根)
    os.makedirs(输出根, exist_ok=True)
    打印(f"输出目录：{输出根}")


def 找打包环境():
    """找装好 PyInstaller 的环境。

    ★ 为什么要找好几个地方：
      打包工具装在哪儿取决于人怎么干活的——
      可能装在项目目录（.venv-build），
      也可能装在别处（工具环境，不污染项目），
      还可能是系统 Python 直接 pip install --user。
      脚本只认一个路径，就会让「在我机器上能打、到你机器上打不了」。
      这正是交付工程的核心教训：**假设会失败，做探测而不是假设。**
    """
    候选 = [
        ("项目目录 .venv-build",
         os.path.join(当前目录, ".venv-build", "Scripts", "pyinstaller.exe")),
        ("项目目录 .venv-build（bin）",
         os.path.join(当前目录, ".venv-build", "bin", "pyinstaller")),
        ("系统 PATH", "pyinstaller"),
    ]
    for 来源, 路径 in 候选:
        if 路径 == "pyinstaller":
            from shutil import which
            if which("pyinstaller"):
                return "pyinstaller", 来源
            continue
        if os.path.exists(路径):
            return 路径, 来源

    # ★ 兜底：扫工作区根目录，找任意一个 .venv-build
    #   （打包工具常装在工具环境里，而不是项目里）
    工作区根 = os.path.dirname(os.path.dirname(当前目录))
    if os.path.isdir(工作区根):
        for 名 in sorted(os.listdir(工作区根)):
            for 子 in ("Scripts", "bin"):
                路径 = os.path.join(工作区根, 名, ".venv-build", 子,
                                    "pyinstaller.exe" if 子 == "Scripts"
                                    else "pyinstaller")
                if os.path.exists(路径):
                    return 路径, f"工作区 {名}/.venv-build"

    return None, None


def 打exe():
    """打单文件 exe。"""
    打印("\n=== ① 单文件 exe ===")
    打包器, 来源 = 找打包环境()
    if not 打包器:
        打印("  ✗ 没找到 PyInstaller。任选一种装法：")
        打印("    A) 在项目目录建独立环境（推荐，不污染别的项目）")
        打印("       python -m venv .venv-build")
        打印("       .venv-build\\Scripts\\pip install pyinstaller openai python-dotenv")
        打印("    B) 或装到系统 Python：pip install pyinstaller")
        return False
    打印(f"  打包器：{打包器}（{来源}）")

    临时 = os.path.join(构建目录, "exe")
    if os.path.exists(临时):
        shutil.rmtree(临时)
    os.makedirs(临时, exist_ok=True)

    # 把核心文件复制到临时目录（PyInstaller 在这里做分析）
    for 文件名 in 核心文件:
        源 = os.path.join(当前目录, 文件名)
        if os.path.exists(源):
            shutil.copy2(源, 临时)
        else:
            打印(f"  ⚠ 缺文件，跳过：{文件名}")

    命令 = [
        打包器,
        "--onefile",              # 单文件
        "--name", "简报助手",       # exe 名字（中文，客户好认）
        "--console",              # 保留控制台窗口（要看输出）
        "--clean",
        "--noconfirm",
        # 排除不需要的大依赖（openai 依赖链很长，httpx 那些不一定要全打）
        "--exclude-module", "tkinter",
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        "meeting_prep_agent.py",
    ]

    打印("  分析依赖（这一步慢，1-3 分钟）…")
    成功, 输出 = 跑(命令, cwd=临时)
    if not 成功:
        return False

    产物 = os.path.join(临时, "dist", "简报助手.exe")
    if not os.path.exists(产物):
        打印("  ✗ 没找到产物")
        return False

    目标 = os.path.join(输出根, "dist-win")
    os.makedirs(目标, exist_ok=True)
    最终 = os.path.join(目标, "简报助手.exe")
    shutil.copy2(产物, 最终)

    # exe 旁边放上配置模板和说明——客户拿到就能用
    for 附赠 in ("部署说明.md", "QUICKSTART.md"):
        源 = os.path.join(当前目录, 附赠)
        if os.path.exists(源):
            shutil.copy2(源, os.path.join(目标, 附赠))

    大小 = os.path.getsize(最终) / 1024 / 1024
    打印(f"  ✓ {最终}（{大小:.1f} MB）")
    return True


def 打绿色包():
    """打绿色包：源码 + 启动脚本 + 配置模板。

    ★ 关键决策：绿色包**不内含 venv**。
    因为 venv 里的脚本写死了绝对路径，拷到别的机器上必然坏。
    正确做法是给客户一个 setup 脚本，在客户机器上现场建环境。
    这个坑我在装 AutoGen 时就踩过（依赖污染），
    路径写死的问题同理。
    """
    打印("\n=== ② 绿色包 ===")
    目标 = os.path.join(输出根, "dist-portable")
    if os.path.exists(目标):
        shutil.rmtree(目标)
    os.makedirs(目标)

    for 文件名 in 核心文件 + 随包文档:
        源 = os.path.join(当前目录, 文件名)
        if os.path.exists(源):
            shutil.copy2(源, 目标)

    模板 = os.path.join(当前目录, "config.toml.example")
    if os.path.exists(模板):
        shutil.copy2(模板, 目标)

    # 数据目录（客户数据不进包，现场建）
    os.makedirs(os.path.join(目标, "memory"), exist_ok=True)
    os.makedirs(os.path.join(目标, "briefings"), exist_ok=True)

    # 写启动脚本
    _写启动脚本(目标)
    _写配置模板(目标)
    _写部署说明(目标)
    _写setup脚本(目标)

    打印(f"  ✓ {目标}")
    return True


def _写setup脚本(目录):
    """写首次初始化脚本（建环境 + 装依赖 + 体检）。

    ★ 绿色包**不内含 venv**，必须在客户机器上现场建。
    原因：venv 里的脚本写死了绝对路径（形如
    C:\\Users\\某人\\AppData\\...\\.venv\\Scripts\\python.exe），
    拷到别的机器上必然坏。装 AutoGen 时踩过同类的坑。
    """
    bat = """@echo off
chcp 65001 >nul
rem ============================================================
rem  首次初始化 · 只需跑一次
rem ============================================================

echo [1/4] 检查 Python 版本...
python --version
if errorlevel 1 (
    echo.
    echo [错误] 没找到 Python。请先装 Python 3.10 或更新版本：
    echo        https://www.python.org/downloads/
    echo        安装时记得勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo.
echo [2/4] 建独立环境...
if exist ".venv" (
    echo     已存在，跳过
) else (
    python -m venv .venv
)

echo.
echo [3/4] 装依赖（只需要 3 个包）...
.venv\\Scripts\\python.exe -m pip install --upgrade pip -q
.venv\\Scripts\\python.exe -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。常见原因：网络问题，或需要用国内镜像：
    echo        .venv\\Scripts\\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo.
echo [4/4] 跑体检...
.venv\\Scripts\\python.exe deploy_config.py
if errorlevel 1 (
    echo.
    echo [提示] 体检发现问题，按上面的提示处理。
) else (
    echo.
    echo [完成] 初始化成功！下一步配密钥，然后双击 启动.bat
)

echo.
pause
"""
    with open(os.path.join(目录, "setup.bat"), "w", encoding="utf-8-sig") as f:
        f.write(bat)

    sh = """#!/bin/bash
# ============================================================
#  首次初始化 · 只需跑一次
# ============================================================
set -e
cd "$(dirname "$0")"

echo "[1/4] 检查 Python 版本..."
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "[错误] 没找到 Python。请先装 Python 3.10 或更新版本。"
    exit 1
fi
$PY --version

echo ""
echo "[2/4] 建独立环境..."
if [ ! -d ".venv" ]; then
    $PY -m venv .venv
else
    echo "  已存在，跳过"
fi

# ★ 关键：Windows 的 venv 是 .venv/Scripts/，Linux/macOS 是 .venv/bin/。
#   同一份脚本要在两种系统上都能跑，所以必须探测而不是写死。
#   写死的话，客户在 WSL 或 Git Bash 里跑就会报
#   ".venv/bin/python: No such file or directory"——这坑我真踩过。
if [ -x ".venv/bin/python" ]; then
    PYBIN=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PYBIN=".venv/Scripts/python.exe"
else
    echo "[错误] 虚拟环境建好了但找不到解释器。"
    echo "  目录内容："
    ls -la .venv/ 2>/dev/null || true
    exit 1
fi
echo "  用 $PYBIN"

echo ""
echo "[3/4] 装依赖（只需要 3 个包）..."
$PYBIN -m pip install --upgrade pip -q
$PYBIN -m pip install -r requirements.txt -q

echo ""
echo "[4/4] 跑体检..."
$PYBIN deploy_config.py

echo ""
echo "[完成] 初始化成功！下一步配密钥，然后运行 ./启动.sh"
"""
    with open(os.path.join(目录, "setup.sh"), "w", encoding="utf-8") as f:
        f.write(sh)
    os.chmod(os.path.join(目录, "setup.sh"), 0o755)


def _写启动脚本(目录):
    """写 Windows 启动脚本 .bat 和 Linux 启动脚本 .sh。"""
    bat = """@echo off
chcp 65001 >nul
rem ============================================================
rem  会前简报 Agent · Windows 启动脚本
rem  首次使用请先跑 setup.bat 建环境
rem ============================================================

if not exist ".venv\\Scripts\\python.exe" (
    echo [错误] 还没初始化环境，请先双击运行 setup.bat
    echo.
    pause
    exit /b 1
)

rem ---- 先看 kill switch 状态（被停用就别往下跑）----
.venv\\Scripts\\python.exe kill_switch.py
if errorlevel 1 (
    echo.
    echo [提示] 当前处于停用状态，功能不会执行。
    echo        需要恢复请运行: .venv\\Scripts\\python.exe kill_switch.py on 恢复
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo 用法: 启动.bat --client 客户名 --industry 行业 --topic 议题
    echo.
    pause
    exit /b 0
)

.venv\\Scripts\\python.exe meeting_prep_agent.py %*
if errorlevel 1 (
    echo.
    echo [失败] 请把上面的报错发给技术支持
    pause
)
"""
    with open(os.path.join(目录, "启动.bat"), "w", encoding="utf-8-sig") as f:
        f.write(bat)

    sh = """#!/bin/bash
# ============================================================
#  会前简报 Agent · Linux/macOS 启动脚本
# ============================================================
cd "$(dirname "$0")"

# ★ 探测 venv 的解释器位置：Windows 是 Scripts/，Linux 是 bin/。
#   写死路径的话，在 WSL / Git Bash 里跑会报 "No such file or directory"。
if [ -x ".venv/bin/python" ]; then
    PYBIN=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PYBIN=".venv/Scripts/python.exe"
else
    echo "[错误] 还没初始化环境，或找不到虚拟环境的解释器。"
    echo "        请先运行: bash setup.sh"
    exit 1
fi

# 先看 kill switch 状态（被停用就别往下跑）
if ! "$PYBIN" kill_switch.py; then
    echo ""
    echo "[提示] 当前处于停用状态，功能不会执行。"
    echo "       需要恢复请运行: $PYBIN kill_switch.py on 恢复"
    exit 1
fi

if [ $# -eq 0 ]; then
    echo "用法: ./启动.sh --client 客户名 --industry 行业 --topic 议题"
    exit 0
fi

"$PYBIN" meeting_prep_agent.py "$@"
"""
    with open(os.path.join(目录, "启动.sh"), "w", encoding="utf-8") as f:
        f.write(sh)
    os.chmod(os.path.join(目录, "启动.sh"), 0o755)


def _写配置模板(目录):
    """写 config.toml 模板（客户改这个）。"""
    内容 = """# ============================================================
#  会前简报 Agent · 部署配置
#  改完立即生效，不需要重启程序。
# ============================================================

[llm]
# 大模型接口地址
base_url = "https://api.deepseek.com"
# 模型名
model = "deepseek-v4-flash"
# ★ 密钥建议用环境变量，不要写在这里。
#   Windows:  set DEEPSEEK_API_KEY=sk-xxx
#   Linux:    export DEEPSEEK_API_KEY=sk-xxx
#   实在要写在这里也能用，但这个文件别外传。

[paths]
# 简报输出目录（相对路径以程序目录为基准）
output_dir = "briefings"
# 记忆库目录（留空 = 用程序同级的 memory/）
memory_dir = ""

[safety]
# 单次请求超时（秒）
timeout = 60
# Agent 模式最大轮数
max_rounds = 6
"""
    with open(os.path.join(目录, "config.toml"), "w", encoding="utf-8") as f:
        f.write(内容)


def _写部署说明(目录):
    """写给客户的部署说明。"""
    内容 = """# 部署说明

## 三步跑起来

### 第 1 步 · 初始化环境（只需做一次）

**Windows**：双击 `setup.bat`
**Linux / macOS**：`bash setup.sh`

它会做三件事：建独立 Python 环境 → 装依赖 → 跑一遍体检。

### 第 2 步 · 配密钥（二选一）

**方式 A（推荐）· 环境变量**

Windows：
```
set DEEPSEEK_API_KEY=sk-你的密钥
```
Linux / macOS：
```
export DEEPSEEK_API_KEY=sk-你的密钥
```

**方式 B · 写进配置文件**

打开 `config.toml`，在 `[llm]` 段加一行：
```
api_key = "sk-你的密钥"
```

> ⚠️ 方式 B 的密钥会明文存在文件里。这个文件别外传、别提交到 git。

密钥申请地址：https://platform.deepseek.com/api_keys

### 第 3 步 · 跑

**Windows**：双击 `启动.bat`，后面跟参数
```
启动.bat --client 招商银行 --industry 金融 --topic 数据中台建设
```
**Linux / macOS**：
```
./启动.sh --client 招商银行 --industry 金融 --topic 数据中台建设
```

简报会写到 `briefings/` 目录，文件名带日期。

---

## 出问题了怎么办

### 体检（先跑这个）

```
.venv\\Scripts\\python.exe deploy_config.py     # Windows
./.venv/bin/python deploy_config.py              # Linux
```

它会一次性告诉你：配置从哪读的、密钥有没有配、输出目录能不能写、记忆库在不在。

### 紧急停止

如果现场出了问题，要立刻停止自动执行：

```
.venv\\Scripts\\python.exe kill_switch.py off 客户要求暂停
```

**这个开关是外置文件、每次读文件，所以改完立刻生效，不用重启。**
正在运行的实例会在下一次检查时停下。

恢复：
```
.venv\\Scripts\\python.exe kill_switch.py on 已和客户确认恢复
```

> ★ **请客户亲手按一次 kill switch。**
> 发文档不算培训，客户自己试一次才算。
> FDE 陪跑项目里踩过这个坑：门禁必须是代码不是纪律，
> 因为「上线第 11 天值班人换了」是必然事件。

### 常见问题

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `缺少大模型 API 密钥` | 密钥没配 | 见第 2 步 |
| `✗ 未找到 DEEPSEEK_API_KEY` | 环境变量没生效 | 体检命令会告诉你配置从哪读的 |
| 简报第三段是「⚠ 首次接触」 | 记忆库是空的 | 正常行为，往 `memory/memories.json` 加记录 |
| 简报第一段「⚠ 未找到」 | 客户名写了简称 | 写全称，或用内置别名（招行/平安/招商…） |
| 简报第四段出现 `M0011` | 版本旧 | 找技术支持要最新版 |
| 提示被杀毒软件拦截 | 单文件 exe 常见误报 | 让客户 IT 加白名单，或改用绿色包 |

---

## 目录说明

```
简报助手/
├─ 简报助手.exe        程序本体
├─ config.toml         ★ 配置（密钥、模型、路径）
├─ 启动.bat / 启动.sh   启动脚本
├─ setup.bat / setup.sh 首次初始化
├─ memory/             历史沟通库（客户自己的数据，不随包分发）
├─ briefings/          简报输出
└─ kill_switch.json    紧急停止开关（首次运行后出现）
```
"""
    with open(os.path.join(目录, "部署说明.md"), "w", encoding="utf-8") as f:
        f.write(内容)


def 打docker():
    """打 Docker 镜像。"""
    打印("\n=== ③ Docker ===")
    目标 = os.path.join(输出根, "dist-docker")
    if os.path.exists(目标):
        shutil.rmtree(目标)
    os.makedirs(目标)

    for 文件名 in 核心文件:
        源 = os.path.join(当前目录, 文件名)
        if os.path.exists(源):
            shutil.copy2(源, 目标)

    _写配置模板(目标)
    _写Dockerfile(目标)
    _写docker说明(目标)

    打印(f"  ✓ {目标}")
    打印("    构建: cd dist-docker && docker build -t brief-agent .")
    return True


def _写Dockerfile(目录):
    内容 = """# 会前简报 Agent · Docker 镜像
#
# ★ 为什么用 python:3.12-slim 而不是 3.13：
#   3.13 刚发布不久，某些依赖（尤其带 C 扩展的）可能还没出 wheel，
#   slim 基础镜像上编译会失败。生产镜像要的是「稳」不是「新」。

FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层 → 改代码不用重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码
COPY . .

# 记忆库和简报输出用卷挂出去，客户数据不留在镜像里
VOLUME ["/app/memory", "/app/briefings"]

# 健康检查：容器不健康就不会被派单
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \\
  CMD python deploy_config.py || exit 1

# 停用状态用退出码 3 表示「被 kill switch 停用」
ENTRYPOINT ["python", "meeting_prep_agent.py"]
"""
    with open(os.path.join(目录, "Dockerfile"), "w", encoding="utf-8") as f:
        f.write(内容)


def _写docker说明(目录):
    内容 = """# Docker 部署

## 构建

```bash
cd dist-docker
docker build -t brief-agent:latest .
```

## 运行

```bash
docker run -it --rm \\
  -e DEEPSEEK_API_KEY=sk-你的密钥 \\
  -v $(pwd)/memory:/app/memory \\
  -v $(pwd)/briefings:/app/briefings \\
  brief-agent:latest \\
  --client 招商银行 --industry 金融 --topic 数据中台建设
```

## 停止

```bash
# 停用（不用停容器，下次调用直接返回停用状态）
docker run --rm -v $(pwd):/app brief-agent:latest \\
  python kill_switch.py off 客户要求暂停
```

## ★ 内网环境注意

如果客户机器完全不能连外网：
- 大模型 API 也连不上 → 需要客户内网部署推理服务，
  把 config.toml 的 base_url 指向内网地址
- Docker 镜像要在有网的环境构建好，导出成 tar 拿过去：
  ```bash
  docker save brief-agent:latest | gzip > brief-agent.tar.gz
  # 客户那边
  gunzip -c brief-agent.tar.gz | docker load
  ```
"""
    with open(os.path.join(目录, "Docker说明.md"), "w", encoding="utf-8") as f:
        f.write(内容)


def 打zip():
    """把绿色包打成 zip，方便发给客户。"""
    打印("\n=== 打包成 zip ===")
    源 = os.path.join(输出根, "dist-portable")
    if not os.path.exists(源):
        打印("  ✗ 先跑绿色包")
        return False

    zip路径 = os.path.join(输出根, "会前简报Agent-绿色包.zip")
    with zipfile.ZipFile(zip路径, "w", zipfile.ZIP_DEFLATED) as z:
        for 根, _, 文件列表 in os.walk(源):
            for 文件名 in 文件列表:
                if 文件名 in 禁止文件:
                    打印(f"  ⚠ 跳过禁止文件：{文件名}")
                    continue
                if 文件名.endswith((".pyc", ".bak")) or "__pycache__" in 根:
                    continue
                全路径 = os.path.join(根, 文件名)
                相对 = os.path.relpath(全路径, os.path.dirname(源))
                z.write(全路径, 相对)
    大小 = os.path.getsize(zip路径) / 1024 / 1024
    打印(f"  ✓ {zip路径}（{大小:.1f} MB）")
    return True


def main():
    print("=" * 64)
    print("  会前简报 Agent · 交付包构建")
    print("=" * 64)

    目标 = sys.argv[1:] or ["exe", "portable", "docker", "zip"]

    准备目录()
    结果 = {}

    if "portable" in 目标:
        结果["portable"] = 打绿色包()
    if "exe" in 目标:
        结果["exe"] = 打exe()
    if "docker" in 目标:
        结果["docker"] = 打docker()
    if "zip" in 目标:
        结果["zip"] = 打zip()

    print("\n" + "=" * 64)
    print("  产出")
    print("=" * 64)
    for 名, 成功 in 结果.items():
        print(f"  {'✓' if 成功 else '✗'} {名}")

    if os.path.exists(构建目录):
        shutil.rmtree(构建目录, ignore_errors=True)

    print(f"\n输出目录：{输出根}")
    return 0 if all(结果.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
