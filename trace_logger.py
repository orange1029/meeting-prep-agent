"""
trace_logger.py — Agent 执行链路追踪

解决什么问题？
  Agent 是黑盒。你知道它答对了，但不知道**过程里发生了什么**：
    · 到底调了哪个工具？参数填对了吗？
    · 哪一步最慢？是LLM 慢还是工具慢？
    · token 花在哪？一次回答 3 轮还是 1 轮？
  出问题时更需要一个时间线：这次调工具报错了，
  发生在第几轮、前一轮 LLM 返回了什么、参数是什么。

核心设计原则：**不污染业务代码**
  接入方式有两种，都不改主流程一行逻辑：
    A. 回调模式（推荐）—— 把回调函数塞进 trace 对象
    B. 包装模式         —— 用 with 语句包住要观测的代码块

输出格式：JSONL（每行一个 JSON 对象）
  为什么不用一个大的 JSON？
    · 流式写入——进程被kill 也不丢已写入的记录
    · 可 grep、可 tail、可被任何工具逐行读
    · 追加写入天然支持

字段约定
  session_id  会话唯一标识，一次进程/一次任务一个
  seq         该会话内的序号，从 1 开始，保证能排序
  timestamp   ISO 8601 时间（本地时区）
  elapsed_ms  距会话开始的毫秒数，用来定位慢在哪一步
  event_type  事件类型，见下方 EVENT_* 常量

用法（回调模式，接入 mcp_client_demo.py 就用这个）：
    trace = Tracer(session_id="demo-1")
    trace.event("llm_request", model=MODEL, tools_count=2, round=1)
    with trace.span("tool_call", name="query_customers") as sp:
        result = call_tool(...)
    # 退出 with 时自动记录耗时与长度

用法（包装模式，不需要改业务代码里的任何 print）：
    with trace.span("llm_request", model=MODEL) as sp:
        resp = client.chat.completions.create(...)
    sp.add(usage_prompt=resp.usage.prompt_tokens)
"""

import json
import os
import time
import uuid
from datetime import datetime

# ============ 事件类型常量 ============
EVENT_LLM_REQUEST = "llm_request"
EVENT_LLM_RESPONSE = "llm_response"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_ERROR = "error"
EVENT_SESSION_START = "session_start"
EVENT_SESSION_END = "session_end"


def _now_iso():
    """当前时间的 ISO 8601 字符串，精确到毫秒"""
    return datetime.now().isoformat(timespec="milliseconds")


def _summarize_args(args, limit=200):
    """把参数摘要成一行字符串。避免把整个 payload 塞进 trace"""
    try:
        s = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + f"...(共{len(s)}字)"


def _safe_len(obj):
    """安全地取长度：可能是 str / list / dict / None"""
    try:
        return len(obj)
    except TypeError:
        return None


class Tracer:
    """
    单个会话的追踪器。

    用法一（只记事件）：
        trace = Tracer()
        trace.event(EVENT_LLM_REQUEST, model="x")

    用法二（记区间，自动算耗时）：
        with trace.span(EVENT_TOOL_CALL, name="query_customers") as sp:
            result = do_something()
            sp.add(result_len=len(result))
    """

    def __init__(self, session_id=None, output_dir=".", enabled=True,
                 console=False):
        """
        session_id  不传则自动生成（短 uuid）
        output_dir trace 文件输出目录
        enabled    False 时所有记录直接丢弃（线上环境可用来关闭追踪）
        console     是否同时打印到控制台
        """
        self.session_id = session_id or f"s-{uuid.uuid4().hex[:8]}"
        self.enabled = enabled
        self.console = console
        self.seq = 0
        self.started_at = time.perf_counter()
        self.records = []          # 内存副本，方便程序内断言与调试

        os.makedirs(output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(
            output_dir, f"trace_{stamp}_{self.session_id}.jsonl")
        self._fh = None
        if enabled:
            # a=追加。同一进程内重复打开不会截断已有记录
            self._fh = open(self.path, "a", encoding="utf-8")

    # ---------- 内部 ----------

    def _write(self, rec):
        """写一条记录到 jsonl + 内存 + 可选控制台"""
        self.seq += 1
        rec["session_id"] = self.session_id
        rec["seq"] = self.seq
        rec["elapsed_ms"] = round(
            (time.perf_counter() - self.started_at) * 1000, 1)
        self.records.append(rec)

        if self._fh:
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 每条都flush。追踪的目的就是「崩了也能看到现场」，
            # 缓冲会让最后几条在最坏情况下丢失
            self._fh.flush()

        if self.console:
            print(f"  [trace] {rec['event_type']:14} "
                  f"{json.dumps({k: v for k, v in rec.items()
                                  if k not in ('session_id', 'seq',
                                               'timestamp', 'elapsed_ms')},
                                 ensure_ascii=False)[:120]}")

    # ---------- 公开接口 ----------

    def event(self, event_type, **fields):
        """记录一个瞬时事件"""
        if not self.enabled:
            return
        self._write({
            "timestamp": _now_iso(),
            "event_type": event_type,
            **fields,
        })

    def span(self, event_type, **fields):
        """
        记录一个带耗时的区间。用 with 使用：

            with trace.span(EVENT_LLM_REQUEST, model=MODEL) as sp:
                resp = client.chat.completions.create(...)
                sp.add(usage_prompt=resp.usage.prompt_tokens)

        退出 with 时自动写入 duration_ms，以及你 sp.add() 的所有字段。
        """
        return _Span(self, event_type, fields)

    def close(self, summary=None):
        """结束会话，可附一份汇总"""
        if not self.enabled:
            return self.path
        total_ms = (time.perf_counter() - self.started_at) * 1000
        counts = {}
        for r in self.records:
            et = r["event_type"]
            counts[et] = counts.get(et, 0) + 1
        self.event(
            EVENT_SESSION_END,
            total_ms=round(total_ms, 1),
            event_counts=counts,
            summary=summary or {},
        )
        if self._fh:
            self._fh.close()
            self._fh = None
        return self.path

    def __enter__(self):
        self.event(EVENT_SESSION_START)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 异常退出也要写 session_end，否则 trace 文件看起来是「没跑完」
        if exc_type is not None:
            self.event(EVENT_ERROR,
                       error_type=exc_type.__name__,
                       error=str(exc_val))
        self.close()
        return False


class _Span:
    """span 上下文管理器，由 Tracer.span() 创建"""

    def __init__(self, tracer, event_type, fields):
        self.tracer = tracer
        self.event_type = event_type
        self.fields = dict(fields)
        self.t0 = None

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def add(self, **fields):
        """补充字段，在 with 块内任意位置调用"""
        self.fields.update(fields)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.tracer.enabled:
            return False
        duration = round((time.perf_counter() - self.t0) * 1000, 1)
        if exc_type is not None:
            self.tracer.event(
                EVENT_ERROR,
                error_type=exc_type.__name__,
                error=str(exc_val),
                duration_ms=duration,
                **self.fields,
            )
        else:
            self.tracer._write({
                "timestamp": _now_iso(),
                "event_type": self.event_type,
                "duration_ms": duration,
                **self.fields,
            })
        return False


# ============ 便捷入口 ============

def make_tracer(output_dir=".", session_id=None, console=False):
    """创建一个 tracer。这是接入方唯一需要调的函数"""
    return Tracer(session_id=session_id, output_dir=output_dir,
                  console=console)


def read_trace(path):
    """读回一个 trace 文件，返回 list of dict。调试与分析用"""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def summarize(records):
    """把 trace 汇总成一份可读的时间线，排查问题时最常用"""
    lines = []
    for r in records:
        et = r["event_type"]
        head = f"[{r.get('elapsed_ms', 0):>8.1f}ms] {et:14}"
        if et == EVENT_LLM_REQUEST:
            head += (f"model={r.get('model')} tools={r.get('tools_count')}"
                     f" msgs={r.get('messages_count')}")
        elif et == EVENT_LLM_RESPONSE:
            head += (f"content_len={r.get('content_len')}"
                     f" tool_calls={r.get('tool_calls_count')}"
                     f" tok={r.get('prompt_tokens')}+{r.get('completion_tokens')}")
        elif et == EVENT_TOOL_CALL:
            head += f"{r.get('name')}({r.get('args_summary')})"
        elif et == EVENT_TOOL_RESULT:
            status = "OK" if r.get("ok") else "FAIL"
            head += (f"{r.get('name')} {status}"
                     f" result_len={r.get('result_len')}")
        elif et == EVENT_ERROR:
            head += f"{r.get('error_type')}: {str(r.get('error'))[:60]}"
        else:
            head += json.dumps({k: v for k, v in r.items()
                                if k not in ("session_id", "seq", "timestamp",
                                             "elapsed_ms", "event_type")},
                               ensure_ascii=False)[:70]
        lines.append(head)
    return "\n".join(lines)


if __name__ == "__main__":
    # 自测：不需要 API，验证格式与字段是否正确
    import tempfile

    tmp = tempfile.mkdtemp()
    tr = Tracer(session_id="selftest", output_dir=tmp)

    tr.event(EVENT_LLM_REQUEST, model="test-model", tools_count=2,
             messages_count=1)
    with tr.span(EVENT_LLM_RESPONSE, round=1) as sp:
        time.sleep(0.05)
        sp.add(content_len=128, tool_calls_count=1,
               prompt_tokens=1420, completion_tokens=210)
    with tr.span(EVENT_TOOL_CALL, name="query_customers",
                 args_summary=_summarize_args({"city": "上海"})) as sp:
        result = [{"id": "C-001"}] * 10
        time.sleep(0.02)
        sp.add(ok=True, result_len=_safe_len(str(result)))
    path = tr.close(summary={"note": "selftest"})

    print(f"trace 文件：{path}")
    print("-" * 70)
    print(summarize(read_trace(path)))
    print("-" * 70)

    # 断言关键字段齐全
    recs = read_trace(path)
    required = {"session_id", "seq", "timestamp", "event_type", "elapsed_ms"}
    for r in recs:
        missing = required - set(r)
        assert not missing, f"记录缺字段 {missing}"
    assert [r["seq"] for r in recs] == list(range(1, len(recs) + 1)), \
        "seq 不连续"
    assert len({r["session_id"] for r in recs}) == 1, "session_id 不一致"
    assert isinstance(path, str) and path.endswith(".jsonl"), "扩展名错误"

    # 每行必须是独立可解析的 JSON
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert len(lines) == len(recs), "行数与记录数不符"
    for ln in lines:
        json.loads(ln)

    print(f"\n✓ {len(recs)} 条记录，格式校验全部通过")