"""被测系统(Evaluator)实现 —— 把一个 EvalSample 变成 PRReviewOutput。

两种实现,都满足 ``protocols.Evaluator`` 协议:

- ``MockEvaluator``:纯启发式、确定性(不调 LLM),按 diff 文本模式给 4 个
  Critic 打分。用于快速 CI:可复现、零 API 成本、零网络。
- ``FunctionEvaluator``:把真 ``devbot.review_agent.review_pr`` 包起来(惰性
  import,避免在仅跑 mock 时强依赖 langgraph)。这是 ``--real`` 路径。
"""
from __future__ import annotations

from devbot_eval.domain import (
    CriticResult, Finding, PRReviewInput, PRReviewOutput, RiskLevel,
)
from devbot_eval.sample import EvalSample

# 与 review_agent 一致的 4 个 Critic 与加权(此处独立复制一份常量,
# 避免 MockEvaluator 仅为取权重就强行 import langgraph 链路)。
_CRITIC_NAMES = ["correctness", "design", "security", "readability"]
_CRITIC_WEIGHTS = {
    "correctness": 0.40,
    "design": 0.20,
    "security": 0.30,
    "readability": 0.10,
}
_VETO_THRESHOLD = 80


def _added_lines(diff: str) -> list[tuple[int, str]]:
    """从 unified diff 里抽出新增行,附一个稳定的伪行号(出现顺序)。

    不解析 @@ hunk header(样本 diff 不保证带),改用"第几条新增行"做行号,
    保证同一 diff 永远得到同一组 (line, text),即确定性。
    """
    out: list[tuple[int, str]] = []
    n = 0
    for raw in diff.splitlines():
        if raw.startswith("+") and not raw.startswith("+++"):
            n += 1
            out.append((n, raw[1:].strip()))
    return out


def _guess_file(diff: str, language: str) -> str:
    """从 diff header 里猜文件名,猜不到给个按语言的默认值。"""
    for raw in diff.splitlines():
        if raw.startswith("+++ ") or raw.startswith("--- "):
            path = raw[4:].strip()
            if path and path not in ("/dev/null",):
                # 去掉 a/ b/ 前缀
                if path[:2] in ("a/", "b/"):
                    path = path[2:]
                return path
    return "Main.java" if language == "java" else "main.py"


class MockEvaluator:
    """确定性 Evaluator:不调 LLM,按 diff 文本模式产 4 个 Critic 结果。

    设计目标:同一个 diff 永远得到同一个 PRReviewOutput,让 metric 管道可在
    CI 里复现。启发式覆盖四个 Critic 各自的领域信号:

    - security:``executeQuery`` / ``Statement`` 配 ``+``/``concat`` 字符串拼接 →
      SQL 注入;硬编码密码 / 密钥;``Runtime.exec`` / ``eval`` 命令注入。
    - correctness:``createStatement`` / ``getConnection`` / ``openStream`` 等资源
      未见 ``close``;``.next()`` 未判返回;裸 ``catch`` 吞异常。
    - design:god-method(新增行很多 / 圈复杂度信号多);魔法数。
    - readability:单字符变量名;``// TODO`` / 注释缺失信号。
    """

    name = "mock"

    def evaluate(self, sample: EvalSample) -> PRReviewOutput:
        diff = sample.diff or ""
        low = diff.lower()
        added = _added_lines(diff)
        file = _guess_file(diff, sample.language)

        critics = [
            self._security(file, diff, low, added),
            self._correctness(file, diff, low, added),
            self._design(file, diff, low, added),
            self._readability(file, diff, low, added),
        ]
        return self._aggregate(sample.pr_id, critics)

    # ── 各 Critic 启发式 ────────────────────────────────────────────────
    def _security(self, file, diff, low, added) -> CriticResult:
        findings: list[Finding] = []
        score = 10
        sql_exec = any(k in low for k in ("executequery", "executeupdate", "execute("))
        concat = ("+" in diff and any(k in low for k in ("select ", "insert ", "update ", "delete ", "where"))) \
            or "concat" in low or 'string sql' in low
        if sql_exec and concat:
            score = max(score, 90)
            ln = self._find_line(added, ("executequery", "executeupdate", "select ", "sql"))
            findings.append(Finding(file=file, line=ln, severity="error",
                                    message="Possible SQL injection: query built from string concatenation.",
                                    critic="security"))
        for kw, msg in (("password", "Hardcoded credential / password literal."),
                        ("secret", "Hardcoded secret in source."),
                        ("api_key", "Hardcoded API key in source."),
                        ("apikey", "Hardcoded API key in source.")):
            if kw in low and ("=" in diff or '"' in diff):
                score = max(score, 80)
                ln = self._find_line(added, (kw,))
                findings.append(Finding(file=file, line=ln, severity="error",
                                        message=msg, critic="security"))
                break
        if any(k in low for k in ("runtime.exec", "processbuilder", " eval(", "os.system")):
            score = max(score, 75)
            ln = self._find_line(added, ("runtime.exec", "processbuilder", "eval(", "os.system"))
            findings.append(Finding(file=file, line=ln, severity="error",
                                    message="Potential command injection via dynamic execution.",
                                    critic="security"))
        return self._mk("security", score, findings,
                        "Avoid building queries/commands from untrusted input; use parameterized APIs.")

    def _correctness(self, file, diff, low, added) -> CriticResult:
        findings: list[Finding] = []
        score = 12
        opened = any(k in low for k in ("createstatement", "getconnection", "preparestatement",
                                        "openstream", "new fileinputstream", "new bufferedreader"))
        if opened and "close" not in low and "try (" not in low and "try-with" not in low:
            score = max(score, 70)
            ln = self._find_line(added, ("createstatement", "getconnection", "openstream", "new file"))
            findings.append(Finding(file=file, line=ln, severity="error",
                                    message="Resource opened but never closed (no try-with-resources / close()).",
                                    critic="correctness"))
        if (".next()" in low or "rs.next" in low) and "if" not in low and "while" not in low:
            score = max(score, 60)
            ln = self._find_line(added, (".next()", "rs.next"))
            findings.append(Finding(file=file, line=ln, severity="warn",
                                    message="ResultSet.next() return value not checked before reading row.",
                                    critic="correctness"))
        if "catch" in low and ("{}" in diff.replace(" ", "") or "pass" in low or "// ignore" in low):
            score = max(score, 55)
            ln = self._find_line(added, ("catch",))
            findings.append(Finding(file=file, line=ln, severity="warn",
                                    message="Exception swallowed (empty catch block).",
                                    critic="correctness"))
        if any(k in low for k in (".get(", "[0]")) and "null" not in low and "none" not in low \
                and not findings:
            score = max(score, 35)
        return self._mk("correctness", score, findings,
                        "Close resources with try-with-resources and check return/null before use.")

    def _design(self, file, diff, low, added) -> CriticResult:
        findings: list[Finding] = []
        score = 15
        n_added = len(added)
        # god-method:新增行很多 + 控制流密集 → 一个方法干太多。
        branchy = sum(low.count(k) for k in ("if ", "for ", "while ", "switch ", "case "))
        if n_added >= 40 or branchy >= 8:
            score = max(score, 70)
            ln = added[0][0] if added else None
            findings.append(Finding(file=file, line=ln, severity="warn",
                                    message="God method: large method with many responsibilities (low cohesion / SRP violation).",
                                    critic="design"))
        elif n_added >= 20 or branchy >= 5:
            score = max(score, 45)
            ln = added[0][0] if added else None
            findings.append(Finding(file=file, line=ln, severity="warn",
                                    message="Method is growing large; consider extracting helpers.",
                                    critic="design"))
        # 魔法数:裸数字字面量(排除 0/1)。
        if self._has_magic_number(added):
            score = max(score, 40)
            ln = self._find_line(added, tuple())  # 取第一条新增行
            findings.append(Finding(file=file, line=(added[0][0] if added else None),
                                    severity="info",
                                    message="Magic number literal; extract a named constant.",
                                    critic="design"))
        return self._mk("design", score, findings,
                        "Split large methods, name magic numbers, keep modules cohesive.")

    def _readability(self, file, diff, low, added) -> CriticResult:
        findings: list[Finding] = []
        score = 10
        if self._has_single_char_var(added):
            score = max(score, 55)
            ln = added[0][0] if added else None
            findings.append(Finding(file=file, line=ln, severity="warn",
                                    message="Single-character / cryptic variable name hurts readability.",
                                    critic="readability"))
        if "todo" in low or "fixme" in low or "xxx" in low:
            score = max(score, 40)
            ln = self._find_line(added, ("todo", "fixme", "xxx"))
            findings.append(Finding(file=file, line=ln, severity="info",
                                    message="Unresolved TODO/FIXME left in code.",
                                    critic="readability"))
        # 深嵌套(连续缩进很深的新增行)。
        if any(len(t) - len(t.lstrip()) >= 16 for _, t in [(n, r) for n, r in added]):
            score = max(score, 35)
        return self._mk("readability", score, findings,
                        "Use descriptive names, flatten nesting, resolve TODOs.")

    # ── 工具 ───────────────────────────────────────────────────────────
    @staticmethod
    def _find_line(added: list[tuple[int, str]], needles: tuple[str, ...]):
        for ln, text in added:
            tl = text.lower()
            if not needles or any(nd in tl for nd in needles):
                return ln
        return added[0][0] if added else None

    @staticmethod
    def _has_magic_number(added: list[tuple[int, str]]) -> bool:
        import re
        pat = re.compile(r"(?<![\w.])\d{2,}(?![\w.])")
        for _, text in added:
            for m in pat.findall(text):
                if m not in ("0", "1"):
                    return True
        return False

    @staticmethod
    def _has_single_char_var(added: list[tuple[int, str]]) -> bool:
        import re
        # 形如  int t =  / var x =  / String s =  的单字符声明
        pat = re.compile(r"\b(?:int|var|long|double|float|String|auto|let|const)\s+([a-z])\s*=")
        bare = re.compile(r"^\s*([a-z])\s*=\s*\S")
        for _, text in added:
            if pat.search(text):
                return True
            m = bare.match(text)
            if m and m.group(1) not in ("i", "j", "k", "n", "x", "y"):
                return True
        return False

    @staticmethod
    def _mk(name: str, score: int, findings: list[Finding], suggestion: str) -> CriticResult:
        score = max(0, min(100, int(score)))
        # 简单可复现的置信度:风险越极端越自信(模拟校准曲线,不调 LLM)。
        confidence = round(0.5 + abs(score - 50) / 120.0, 4)
        return CriticResult(
            critic=name, risk_score=score, confidence=confidence, findings=findings,
            suggestion=suggestion, model="mock-evaluator",
            tokens_in=0, tokens_out=0, latency_ms=1, error=None,
        )

    @staticmethod
    def _aggregate(pr_id: str, critics: list[CriticResult]) -> PRReviewOutput:
        weighted = total_w = 0.0
        veto = False
        for cr in critics:
            w = _CRITIC_WEIGHTS.get(cr.critic, 0.1)
            weighted += cr.risk_score * w
            total_w += w
            if cr.risk_score >= _VETO_THRESHOLD:
                veto = True
        final = int(weighted / total_w) if total_w else 50
        if veto:
            final = max(final, _VETO_THRESHOLD)
        level = RiskLevel.from_score(final)
        summary = " | ".join(
            f"{cr.critic}: {cr.risk_score} ({RiskLevel.from_score(cr.risk_score).value})"
            for cr in critics
        )
        if veto:
            summary += " [VETO triggered]"
        return PRReviewOutput(
            pr_id=pr_id, risk_score=final, risk_level=level, critics=critics,
            summary=summary, total_tokens=0, total_cost_usd=0.0, total_latency_ms=1,
        )


class FunctionEvaluator:
    """``--real`` 路径:把真 ``devbot.review_agent.review_pr`` 包成 Evaluator。

    惰性 import review_agent / PRReviewInput,这样仅跑 mock 的 CI 不必装
    langgraph。可传入自建的 ``llm`` / ``codedoc`` 注入真客户端;默认让
    review_pr 自己建(读 config 的 provider,默认 openai)。
    """

    name = "function"

    def __init__(self, llm=None, codedoc=None):
        self._llm = llm
        self._codedoc = codedoc

    def evaluate(self, sample: EvalSample) -> PRReviewOutput:
        # 惰性 import:避免 mock-only CI 强依赖 langgraph。
        from devbot.review_agent import review_pr

        pr_input = PRReviewInput(
            pr_id=sample.pr_id,
            diff=sample.diff or "",
            impact_files=[],
            title=sample.title or "",
            description="",
            language=sample.language or "python",
        )
        return review_pr(pr_input, llm=self._llm, codedoc=self._codedoc)
