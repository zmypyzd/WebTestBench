import os
import json
import time
from pathlib import Path
from typing import Any, Callable, Optional, Dict, List
from dataclasses import asdict
from claude_agent_sdk import (
    query, ClaudeAgentOptions, 
    UserMessage, AssistantMessage, ResultMessage,
    TextBlock, ToolUseBlock, ToolResultBlock
)

from agent import APIConfig, BaseAgent
from agent.reverify_reconcile import parse_pass_items, build_sub_checklist, reconcile, filter_to_classes, has_canonical_items
from prompt import USER_PROMPT
from tools import PlaywrightTools
from utils import *


class ClaudeCodeWebTester(BaseAgent):
    """
    Baseline two-step agent:
    1) Generate checklist from development instruction.
    2) Defect Detection: Execute actions from checklist on the target page and return testing results.
    """

    def __init__(
        self,
        instruction: str,
        api_config: APIConfig,
        server_url: str,
        local_project_dir: Optional[str] = None,
        output_dir: str | Path = "./output/results",
        event_log_stream: Optional[Any] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            instruction=instruction,
            api_config=api_config,
            output_dir=output_dir,
            server_url=server_url,
            local_project_dir=local_project_dir,
            event_log_stream=event_log_stream,
            require_evidence=bool(kwargs.get("require_evidence", False)),
        )

        self.checklist_path = self.output_dir / "checklist.md"
        self.result_path = self.output_dir / "result.md"
        self.result_reverify_raw_path = self.output_dir / "result_reverify_raw.md"
        # BaseAgent declares reverify_enabled/result_reverified_path; the gate value
        # arrives via kwargs from run_agent.py (constructor previously dropped kwargs).
        self.reverify_enabled = bool(kwargs.get("reverify", False))
        # Classes whose PASS items get re-verified/flippable. CS/IX are the diagnosed
        # misjudgment classes; FT/CT were pure FP risk in the n=1 smoke.
        self.reverify_classes = ("CS", "IX")
        self.message_class_counts: Dict[str, Dict[str, Any]] = {}
        self.session_success = True
        self.recent_assistant_text_blocks: Dict[str, List[str]] = {}
        # defect detection stage setting
        self.max_turns = 150
        # defect_hunt stage: number of adversarial rounds (0 disables the stage).
        self.hunt_rounds = int(kwargs.get("hunt_rounds", 3))

        self.cwd_dir = "./claude_code_cwd"
        os.makedirs(self.cwd_dir, exist_ok=True)


    async def run(self) -> bool:
        """Run checklist generation then execution."""
        eval_done = self.result_extracted_path.exists()
        hunt_pending = self.hunt_rounds > 0 and not self.bugs_path.exists()
        if eval_done and not hunt_pending:
            self._should_skip_stage(self.result_extracted_path, stage="eval")  # emit skip event only when actually skipping
            return True
        
        self._log_instruction()

        start_ts = time.time()
        stage_sequence = [
            self.server_deploy,
            self.checklist_generation,
            self.defect_detection,
            self.defect_reverify,
            self.extract_result_file,
            self.defect_hunt,
        ]

        success = True
        try:
            for stage_callable in stage_sequence:
                stage_result = await stage_callable()
                if stage_result is False:
                    success = False
                    break
        finally:
            end_ts = time.time()
            duration = end_ts - start_ts
            self.kill_local_server()

        completion_message = "✅ Web Testing completed." if success else "❌ Web Testing encountered errors."
        (print_green if success else print_red)(completion_message)
        self._emit_event(
            type_name="pipeline_status", stage="finish", status="complete" if success else "error", message=completion_message,
            payload=dict(
                start_time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts)),
                end_time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_ts)),
                duration=time.strftime("%H:%M:%S", time.gmtime(int(round(duration or 0)))),
            )
        )
        return success

    async def checklist_generation(self) -> bool:
        stage = "checklist_generation"
        target_file = self.checklist_path
        self.current_stage = stage

        if self._should_skip_stage(target_file, stage):
            return True

        self._write_stage_success(stage, True)
        self._mark_stage(stage=stage, status="running", message="🚀 Checklist Generation ...")
        prompt = USER_PROMPT["checklist_generation"].substitute(instruction=self.instruction)
        self.event_log_stream.write(f"{'-'*20} USER PROMPT {'-'*20}\n{prompt}\n{'-'*50}\n")
        options = self._get_chat_agent_options(max_turns=5)

        async for message in query(prompt=prompt, options=options):
            self._log_session_id(message, session_name=stage, stage=stage, prompt=prompt)
            self._handle_message(message, stage=stage)
            if isinstance(message, ResultMessage):
                result_message = message.result

        final_result, from_result_message = self._extract_final_result(result_message, stage=stage)
        self._record_final_result_source(stage, from_result_message)
        if final_result == "":
            self._mark_stage(stage=stage, status="error", message=f"Stage {stage} produced invalid checklist content; missing '# Test Checklist'.",)
            return False

        self.write_markdown(target_file, final_result)

        if self._verify_output_file(target_file):
            self._emit_file_event(stage, target_file)
            print_green("✅ Checklist Generation Completed.")
            return True
        else:
            self._mark_stage(stage=stage, status="error", message=f"Stage {stage} did not produce {target_file}.")
            return False

    async def defect_detection(self) -> bool:
        stage = "defect_detection"
        target_file = self.result_path
        self.current_stage = stage

        if self._should_skip_stage(target_file, stage):
            return True

        if not self.checklist_path.exists():
            self._mark_stage(stage=stage, status="error", message="Checklist not found; cannot execute actions.")
            return False

        self._write_stage_success(stage, True)
        self._mark_stage(stage=stage, status="running", message="🚀 Defect Detection ...")
        checklist_md = self._load_file_content(self.checklist_path)
        prompt = USER_PROMPT["defect_detection"].substitute(
            instruction=self.instruction, server_url=self.server_url, checklist=checklist_md,
        )
        if getattr(self, "require_evidence", False):
            from prompt.defect_detection import EVIDENCE_REQUIREMENT
            prompt = prompt + EVIDENCE_REQUIREMENT
        options = self._get_browser_agent_options(max_turns=self.max_turns)

        async for message in query(prompt=prompt, options=options):
            self._log_session_id(message, session_name=stage, stage=stage, prompt=prompt)
            self._handle_message(message, stage=stage)
            if isinstance(message, ResultMessage):
                result_message = message.result
                num_turns = message.num_turns
        
        if num_turns > self.max_turns:
            self.write_markdown(target_file, "")
            self.write_markdown(self.result_extracted_path, "")
        else:
            final_result, from_result_message = self._extract_final_result(result_message, stage=stage)
            self._record_final_result_source(stage, from_result_message)
            self.write_markdown(target_file, final_result)
            if final_result == "":
                self._mark_stage(stage=stage, status="error", message=f"Stage {stage} produced invalid result content; missing '# Test Result'.",)
                return False

        if self._verify_output_file(target_file):
            self._emit_file_event(stage, target_file)
            if getattr(self, "require_evidence", False):
                from agent.evidence_lint import find_unsupported_pass
                try:
                    result_text = self.result_path.read_text(encoding="utf-8")
                except Exception:
                    result_text = ""
                offenders = find_unsupported_pass(result_text)
                self._emit_event(
                    type_name="evidence_lint",
                    stage=stage,
                    status=None,
                    message=f"{len(offenders)} PASS item(s) lack Evidence: {offenders}",
                )
            print_green("✅ Action Execution Completed.")
            return True
        else:
            self._mark_stage(stage=stage, status="error", message=f"Stage {stage} did not produce {target_file}.")
            return False

    async def defect_reverify(self) -> bool:
        stage = "defect_reverify"
        target_file = self.result_reverified_path
        self.current_stage = stage

        # Gate: no-op when disabled -> baseline byte-identical.
        if not self.reverify_enabled:
            return True

        if self._should_skip_stage(target_file, stage):
            return True

        if not self.result_path.exists():
            # No first-pass result to build on or fall back to: genuine hard error
            # (baseline could not score this record either).
            self._mark_stage(stage=stage, status="error", message="reverify needs result.md.")
            return False
        if not self.checklist_path.exists():
            # Can't build a sub-checklist, but we must not regress a scorable record to
            # unscored: degrade to the first pass instead of aborting the pipeline.
            return self._reverify_degrade(
                stage, target_file, self._load_file_content(self.result_path),
                "reverify: checklist.md missing; kept first pass.",
            )

        self._write_stage_success(stage, True)
        self._mark_stage(stage=stage, status="running", message="🚀 Defect Re-Verify ...")

        pass1_text = self._load_file_content(self.result_path)
        checklist_md = self._load_file_content(self.checklist_path)

        # Non-canonical first-pass format (e.g. heading-style '### FT-01 ... PASS') is
        # invisible to our checkbox parser, so reverify cannot run on it. Surface this
        # LOUDLY instead of silently looking like "ran, nothing to flip" — detection
        # format variance is an upstream gap, not a clean no-op.
        if not has_canonical_items(pass1_text):
            self._emit_event(type_name="reverify_unparseable", stage=stage,
                             payload=dict(result_chars=len(pass1_text)))
            self._mark_stage(stage=stage, status="skip",
                             message="reverify: first-pass result not in canonical checkbox format; skipped (kept first pass).")
            self.write_markdown(target_file, pass1_text)
            self._emit_file_event(stage, target_file)
            return True

        pass_ids = parse_pass_items(pass1_text)
        # Scope re-verification to the classes where detection actually misjudges
        # (CS/IX per the P2 diagnosis). FT/CT re-verification was empirically pure
        # FP risk with no recall gain (n=1 smoke 0002: FT-02 flipped as a false alarm).
        pass_ids = filter_to_classes(pass_ids, self.reverify_classes)

        # Canonical items exist but none in scope PASS -> genuinely nothing to re-verify.
        if not pass_ids:
            self.write_markdown(target_file, pass1_text)
            self._emit_file_event(stage, target_file)
            print_green("✅ Re-Verify skipped (no in-scope PASS items).")
            return True

        sub_checklist, dropped = build_sub_checklist(checklist_md, pass_ids)
        if dropped:
            self._emit_event(type_name="reverify_dropped_ids", stage=stage,
                             payload=dict(count=len(dropped), ids=dropped))
        if sub_checklist.strip() == "# Test Checklist":
            # All PASS ids drifted; nothing concrete to re-test -> keep first pass.
            self.write_markdown(target_file, pass1_text)
            self._emit_file_event(stage, target_file)
            return True

        prompt = USER_PROMPT["defect_reverify"].substitute(
            instruction=self.instruction, server_url=self.server_url, checklist=sub_checklist,
        )
        if self.event_log_stream:
            self.event_log_stream.write(f"{'-'*20} REVERIFY PROMPT {'-'*20}\n{prompt}\n{'-'*50}\n")
        options = self._get_browser_agent_options(max_turns=self.max_turns)

        result_message = ""
        num_turns = 0
        async for message in query(prompt=prompt, options=options):
            self._log_session_id(message, session_name=stage, stage=stage, prompt=prompt)
            self._handle_message(message, stage=stage)
            if isinstance(message, ResultMessage):
                result_message = message.result
                num_turns = message.num_turns

        # Degrade on any failure: reconciled := first pass (never destroy caught bugs).
        if num_turns > self.max_turns:
            return self._reverify_degrade(stage, target_file, pass1_text, "reverify exceeded turn budget; kept first pass.")

        reverify_raw, from_result_message = self._extract_final_result(result_message, stage=stage)
        self._record_final_result_source(stage, from_result_message)
        self.write_markdown(self.result_reverify_raw_path, reverify_raw)

        if not self._has_required_result(reverify_raw):
            return self._reverify_degrade(stage, target_file, pass1_text, "reverify output missing '# Test Result'; kept first pass.")

        try:
            reconciled, stats = reconcile(pass1_text, reverify_raw)
        except Exception as exc:
            return self._reverify_degrade(stage, target_file, pass1_text, f"reconcile failed ({exc}); kept first pass.")

        self._emit_event(type_name="reverify_flips", stage=stage,
                         payload=dict(flipped=stats["flipped"], considered=stats["considered"]))
        self.write_markdown(target_file, reconciled)

        if self._verify_output_file(target_file):
            self._emit_file_event(stage, target_file)
            print_green(f"✅ Re-Verify Completed (flipped {len(stats['flipped'])}/{stats['considered']}).")
            return True
        self._mark_stage(stage=stage, status="error", message=f"Stage {stage} did not produce {target_file}.")
        return False

    def _reverify_degrade(self, stage: str, target_file, pass1_text: str, reason: str) -> bool:
        """Keep the first-pass result when re-verify cannot safely improve on it."""
        self.write_markdown(target_file, pass1_text)
        self._mark_stage(stage=stage, status="error", message=reason)
        self._emit_file_event(stage, target_file)
        return True

    # Best-effort, dual-track stage: runs LAST (server still alive), always returns True so it
    # never flips the pipeline success flag, and writes only BUGS.md (never feeds scoring).
    async def defect_hunt(self) -> bool:
        stage = "defect_hunt"
        target_file = self.bugs_path
        self.current_stage = stage

        if self.hunt_rounds <= 0:
            return True
        if self._should_skip_stage(target_file, stage):
            return True

        try:
            self._write_stage_success(stage, True)
            self._mark_stage(stage=stage, status="running", message="🔪 Defect Hunt (chaos-qa) ...")

            project_dir = os.path.abspath(self.local_project_dir) if self.local_project_dir else "."
            prompt = USER_PROMPT["defect_hunt"].substitute(
                instruction=self.instruction,
                server_url=self.server_url,
                project_dir=project_dir,
                hunt_rounds=self.hunt_rounds,
            )
            options = self._get_browser_agent_options(max_turns=self.max_turns)

            result_message = ""
            num_turns = 0
            async for message in query(prompt=prompt, options=options):
                self._log_session_id(message, session_name=stage, stage=stage, prompt=prompt)
                self._handle_message(message, stage=stage)
                if isinstance(message, ResultMessage):
                    result_message = message.result
                    num_turns = message.num_turns

            if num_turns > self.max_turns:
                self.write_markdown(target_file, "")
            else:
                final_result, from_result_message = self._extract_final_result(result_message, stage=stage)
                self._record_final_result_source(stage, from_result_message)
                # Models often prepend a conversational preamble ("Here is the report:")
                # before the actual report; trim it so BUGS.md starts at the header.
                self.write_markdown(target_file, self._slice_bug_report(final_result))

            if self._verify_output_file(target_file):
                self._emit_file_event(stage, target_file)
                print_green("✅ Defect Hunt Completed.")
            else:
                self._mark_stage(stage=stage, status="error", message=f"Stage {stage} did not produce {target_file}.")
        except Exception as exc:
            self._mark_stage(stage=stage, status="error", message=f"Stage {stage} raised and was suppressed (best-effort): {exc}")
        return True

    # ------------------------------------------------------------------ #
    # Conversation Helpers
    # ------------------------------------------------------------------ #

    def _log_session_id(self, message, session_name: str, stage: str, prompt: str, extra_meta: Optional[Dict] = None):
        if hasattr(message, "subtype") and message.subtype == "init":
            session_id = message.data.get("session_id")
            session_meta: Dict[str, Any] = {}
            if self.session_meta_path.exists():
                session_meta = json.loads(self.session_meta_path.read_text(encoding="utf-8"))
            if not isinstance(session_meta, dict):
                session_meta = {}

            session_entry = {"stage": stage, "session_id": session_id, "user_prompt": prompt}
            if extra_meta and isinstance(extra_meta, dict):
                session_entry.update(extra_meta)

            session_meta[session_name] = session_entry
            self.session_meta_path.write_text(json.dumps(session_meta, ensure_ascii=False, indent=2), encoding="utf-8")

            self._emit_event(
                type_name="log_session_id", stage=stage, payload=dict(session_id=session_id, extra_meta=extra_meta)
            )
    
    def _handle_message(self, message, stage: str):
        """Emit structured events while streaming assistant messages and tool usage."""
        self._record_message_count(message, stage=stage)

        text_content = ""
        if isinstance(message, UserMessage):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    self._emit_event(
                        type_name="message",
                        stage=stage,
                        payload=dict(role="user", type="user_tool_result", content=block.content)
                    )
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_content += block.text
                    self._emit_event(
                        type_name="message",
                        stage=stage,
                        payload=dict(role="assistant", type="assistant_message", content=block.text)
                    )

                    recent_blocks = self.recent_assistant_text_blocks.get(stage)
                    if not isinstance(recent_blocks, list):
                        recent_blocks = []
                        self.recent_assistant_text_blocks[stage] = recent_blocks
                    recent_blocks.append(block.text)
                    if len(recent_blocks) > 5:
                        del recent_blocks[:-5]

                elif isinstance(block, ToolUseBlock):
                    self._emit_event(
                        type_name="message",
                        stage=stage,
                        payload=dict(role="assistant", type="assistant_tool", content=block.name)
                    )
        
        elif isinstance(message, ResultMessage):
            print("Result message received.")
            self._emit_event(
                type_name="message",
                stage=stage,
                payload=dict(role="assistant", type="result_message", content=asdict(message))
            )

            cost_content = (
                f"total_cost_usd: {message.total_cost_usd}\n"
                f"token usage: {json.dumps(message.usage, ensure_ascii=False, indent=2)}"
            )
            print_boxed(cost_content)

            session_meta: Dict[str, Any] = {}
            if self.session_meta_path.exists():
                try:
                    session_meta = json.loads(self.session_meta_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    print_red(f"Failed to read session meta from {self.session_meta_path}: {exc}")
            if not isinstance(session_meta, dict):
                session_meta = {}

            stage_entry = session_meta.get(stage)
            if not isinstance(stage_entry, dict):
                stage_entry = {}

            stage_entry.update(
                {
                    "duration_ms": message.duration_ms,
                    "is_error": message.is_error,
                    "num_turns": message.num_turns,
                    "total_cost_usd": message.total_cost_usd,
                    "token_usage": message.usage,
                }
            )
            session_meta[stage] = stage_entry

            try:
                self.session_meta_path.write_text(
                    json.dumps(session_meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:
                print_red(f"Failed to write session meta to {self.session_meta_path}: {exc}")
        
        return text_content

    def _extract_final_result(self, result_text: str, stage: str) -> tuple[str, bool]:
        check_final_fun = {
            "checklist_generation": self._has_required_checklist,
            "defect_detection": self._has_required_result,
            "defect_reverify": self._has_required_result,
            "defect_hunt": self._has_required_bugs,
        }
        if stage in check_final_fun.keys():
            if check_final_fun[stage](result_text):
                return result_text, True
            
            recent_blocks = self.recent_assistant_text_blocks.get(stage, [])
            for candidate in reversed(recent_blocks):
                if check_final_fun[stage](candidate):
                    return candidate, False
            else:
                return result_text, True
        
        return result_text, True

    def _record_final_result_source(self, stage: str, from_result_message: bool) -> None:
        session_meta: Dict[str, Any] = {}
        if self.session_meta_path.exists():
            try:
                session_meta = json.loads(self.session_meta_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print_red(f"Failed to read session meta from {self.session_meta_path}: {exc}")
        if not isinstance(session_meta, dict):
            session_meta = {}

        final_result_sources = session_meta.get("final_result_sources")
        if not isinstance(final_result_sources, dict):
            final_result_sources = {}

        final_result_sources[stage] = from_result_message
        session_meta["final_result_sources"] = final_result_sources

        try:
            self.session_meta_path.write_text(
                json.dumps(session_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            print_red(f"Failed to write session meta to {self.session_meta_path}: {exc}")
    
    def _record_message_count(self, message, stage: str) -> None:
        msg_class = type(message).__name__
        stage_counts = self.message_class_counts.get(stage)
        if not isinstance(stage_counts, dict):
            stage_counts = {}
            self.message_class_counts[stage] = stage_counts

        if msg_class in ("UserMessage", "AssistantMessage"):
            entry = stage_counts.get(msg_class)
            if not isinstance(entry, dict):
                entry = {"total_count": 0}
                stage_counts[msg_class] = entry
            entry["total_count"] = entry.get("total_count", 0) + 1
            if hasattr(message, "content"):
                for block in message.content:
                    block_class = type(block).__name__
                    entry[block_class] = entry.get(block_class, 0) + 1
        else:
            stage_counts[msg_class] = stage_counts.get(msg_class, 0) + 1

        self._write_message_statistics(stage)

    def _write_message_statistics(self, stage: str) -> None:
        session_meta: Dict[str, Any] = {}
        if self.session_meta_path.exists():
            try:
                session_meta = json.loads(self.session_meta_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print_red(f"Failed to read session meta from {self.session_meta_path}: {exc}")
        if not isinstance(session_meta, dict):
            session_meta = {}

        message_statistics = session_meta.get("message_statistics")
        if not isinstance(message_statistics, dict):
            message_statistics = {}

        message_statistics[stage] = dict(self.message_class_counts.get(stage, {}))
        session_meta["message_statistics"] = message_statistics

        try:
            self.session_meta_path.write_text(
                json.dumps(session_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            print_red(f"Failed to write session meta to {self.session_meta_path}: {exc}")

    # ------------------------------------------------------------------ #
    # Agent configuration
    # ------------------------------------------------------------------ #

    def _provider_env(self) -> Dict[str, str]:
        """Environment overrides for the Claude Agent SDK subprocess.

        - When base_url AND api_key are both set, redirect the SDK to a custom
          Anthropic-compatible provider (e.g. OpenRouter / MiniMax).
        - When both are empty, return {} so the SDK falls back to the locally
          logged-in Claude Code CLI credentials (native Anthropic models).
        """
        if self.api_config.base_url and self.api_config.api_key:
            return {
                "ANTHROPIC_BASE_URL": self.api_config.base_url,
                "ANTHROPIC_AUTH_TOKEN": self.api_config.api_key,
                "ANTHROPIC_API_KEY": "",
            }
        return {}

    def _get_chat_agent_options(
        self, 
        system_prompt: Optional[str] = None,
        max_turns: int = 5,
        max_buffer_size: int = 1024*1024,
    ) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=[],  # chat-only checklist stage needs no tools (SDK >=0.2 requires a list, not None)
            model=self.api_config.model,
            max_turns=max_turns,
            max_buffer_size=max_buffer_size,
            cwd=self.cwd_dir,
            env=self._provider_env(),
        )

    def _get_browser_agent_options(
        self, 
        system_prompt: Optional[str] = None,
        max_turns: int = 5,
        max_buffer_size: int = 1024*1024,
    ) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={
                "playwright": {
                    "type": "stdio",
                    "command": "npx",
                    "args": [
                        "-y", "@playwright/mcp@0.0.61", 
                        "--isolated",
                        "--headless",
                        "--viewport-size", "1280,720",
                    ]
                }
            },
            # Playwright MCP (preferred) PLUS sanctioned white-box tools: reading
            # the app's source/seed data is explicitly allowed (only the gold
            # answer is off-limits). NOTE: allowed_tools is an auto-approve list,
            # not an availability gate, so Bash was already reachable; we make the
            # policy explicit and block mutation/screenshot via disallowed_tools.
            allowed_tools=PlaywrightTools + ["Bash", "Read", "Grep", "Glob"],
            disallowed_tools=[
                "mcp__playwright__browser_take_screenshot",
                "Write",
                "Edit",
            ],
            model=self.api_config.model,
            max_turns=max_turns,
            max_buffer_size=max_buffer_size,
            cwd=self.cwd_dir,
            env=self._provider_env(),
        )
