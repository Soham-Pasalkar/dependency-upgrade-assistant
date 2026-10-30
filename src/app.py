"""Streamlit Web Application: Dependency Upgrade Assistant & Repository Improvement Platform."""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

# Ensure project root is in sys.path when invoked via `streamlit run src/app.py`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from src.analysis.code_improver import ProposedImprovement
from src.core.comparator import compare_requirements
from src.core.diff_generator import generate_unified_diff, synthesize_updated_requirements
from src.core.models import UpgradeStatus
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog, load_catalog, normalize_package_name
from src.pipeline.orchestrator import (
    PipelineOrchestrator,
    PipelineResult,
    PipelineStage,
    StageRecord,
    StageStatus,
)
from src.services.git_service import GitService, extract_repo_components, validate_github_url
from src.services.ollama_client import OllamaClient
from src.services.workspace_manager import WorkspaceManager, WorkspaceSession
from src.validation.sandbox_manager import DockerSandboxManager
from src.validation.test_runner import ValidationStatus


# Page configuration
st.set_page_config(
    page_title="Dependency Upgrade Assistant",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Constants & Paths
MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1 MB
SAMPLE_DATA_DIR = Path(__file__).parent.parent / "sample-data"


@st.cache_resource
def get_catalog() -> Catalog:
    """Load and cache the local package catalog."""
    return load_catalog()


@st.cache_resource
def get_ollama_client() -> OllamaClient:
    """Load and cache the Ollama client instance."""
    return OllamaClient()


def load_sample_file(filename: str) -> str:
    """Load content of a sample file from sample-data directory."""
    path = SAMPLE_DATA_DIR / filename
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


# ===========================================================================
# TAB 1: Phase 2 - Repository Analysis & Improvement Platform
# ===========================================================================

def render_phase2_repository_tab(catalog: Catalog) -> None:
    """Render Tab 1: Repository Analysis, AI Improvements, Sandbox Validation & Export."""
    st.subheader("Repository Analysis & Improvement")
    st.markdown(
        "Analyze a GitHub repository to discover dependency manifests, evaluate upgrades with live PyPI data, "
        "inspect source code for bugs and security hotspots with local Ollama AI models, "
        "and validate approved changes safely in an isolated Docker sandbox."
    )

    ollama_client = get_ollama_client()
    ollama_health = ollama_client.check_health()
    sandbox_mgr = DockerSandboxManager()
    docker_avail = sandbox_mgr.check_availability()

    # --- Step 1: Repository & Runtime Configuration ---
    with st.container():
        col_url, col_auth = st.columns([3, 2])

        with col_url:
            repo_url = st.text_input(
                "GitHub Repository URL:",
                placeholder="https://github.com/psf/requests",
                help="HTTPS clone URL of a public or private GitHub repository.",
                key="p2_repo_url",
            )

        with col_auth:
            is_private = st.checkbox(
                "Private Repository (Requires Token)",
                value=False,
                help="Check to provide a personal access token. Kept in memory only; never logged or saved to disk.",
                key="p2_is_private",
            )
            pat_token: Optional[str] = None
            if is_private:
                pat_token = st.text_input(
                    "GitHub Personal Access Token (PAT):",
                    type="password",
                    help="Held in memory exclusively for git clone via GIT_ASKPASS.",
                    key="p2_pat_token",
                )

        # Advanced Settings Expander
        with st.expander("Runtime & AI Settings", expanded=True):
            col_model, col_limit, col_sandbox = st.columns([2, 1, 2])

            with col_model:
                if ollama_health.is_available:
                    models = ollama_client.list_model_names()
                    if models:
                        selected_model = st.selectbox(
                            "Ollama AI Model:",
                            options=models,
                            index=0,
                            help="Local Ollama models discovered via /api/tags.",
                            key="p2_model_select",
                        )
                    else:
                        selected_model = None
                        st.info("Ollama is running, but no models were detected. Run `ollama pull <model>` to enable AI analysis.")
                else:
                    selected_model = None
                    st.caption("Ollama service not detected at `http://localhost:11434`. Deterministic analysis enabled.")

            with col_limit:
                inspection_limit = st.number_input(
                    "Max Inspection Files:",
                    min_value=1,
                    max_value=50,
                    value=10,
                    step=1,
                    help="Priority-ordered source files to inspect (default: 10).",
                    key="p2_inspection_limit",
                )

            with col_sandbox:
                if docker_avail.is_available:
                    st.caption(f"Docker Sandbox: **Active** (`{docker_avail.version or 'available'}`)")
                else:
                    st.caption("Docker Sandbox: **Unavailable**  \nRepository tests will safely report as `NOT_RUNNABLE` without executing on the host.")

    # --- Step 2: Primary Action Trigger ---
    st.divider()
    btn_col, _ = st.columns([2, 5])
    with btn_col:
        analyze_clicked = st.button(
            "Analyze Repository",
            type="primary",
            use_container_width=True,
            key="p2_analyze_btn",
        )

    # Clean previous workspace if starting new analysis
    if analyze_clicked:
        if not repo_url or not repo_url.strip():
            st.error("Please enter a valid GitHub repository URL.")
            return

        if not validate_github_url(repo_url.strip()):
            st.error("Invalid GitHub URL format. Please use a canonical HTTPS format: `https://github.com/owner/repository`")
            return

        # Clean existing session workspace if present
        existing_ws: Optional[WorkspaceSession] = st.session_state.get("p2_workspace")
        if existing_ws:
            existing_ws.cleanup()
            st.session_state["p2_workspace"] = None

        # Reset intermediate states
        st.session_state["p2_analysis_result"] = None
        st.session_state["p2_final_result"] = None

        ws_mgr = WorkspaceManager()
        ws = ws_mgr.create_workspace()
        st.session_state["p2_workspace"] = ws

        orchestrator = PipelineOrchestrator(
            ollama_client=ollama_client,
            catalog=catalog,
            sandbox_manager=sandbox_mgr,
        )

        with st.status("Analyzing Repository...", expanded=True) as status_box:
            def on_progress(stage: PipelineStage, status: StageStatus, message: str, details=None):
                icon = "⏳" if status == StageStatus.RUNNING else ("✅" if status == StageStatus.COMPLETED else ("❌" if status == StageStatus.FAILED else "⏭️"))
                status_box.write(f"{icon} **{stage.value}**: {message}")

            analysis_result = orchestrator.run_analysis_stages(
                ws=ws,
                repo_url=repo_url.strip(),
                pat_token=pat_token if is_private else None,
                ollama_model=selected_model,
                inspection_limit=int(inspection_limit),
                progress_callback=on_progress,
            )

            st.session_state["p2_analysis_result"] = analysis_result
            st.session_state["p2_active_model"] = selected_model

            if analysis_result.is_partial or analysis_result.error:
                status_box.update(label="Repository Analysis Incomplete or Encountered an Error.", state="error", expanded=True)
            else:
                status_box.update(label="Repository Analysis Complete! Review Findings and Approve Changes Below.", state="complete", expanded=False)

    # --- Step 3: Display Findings & Human Approval Interface ---
    analysis_result: Optional[PipelineResult] = st.session_state.get("p2_analysis_result")
    if analysis_result:
        st.divider()

        # Metrics cards
        m_col1, m_col2, m_col3, m_col4, m_col5 = st.columns(5)
        with m_col1:
            st.metric("Manifests", len(analysis_result.manifests))
        with m_col2:
            total_deps = analysis_result.dependency_report.total_dependencies if analysis_result.dependency_report else 0
            st.metric("Dependencies", total_deps)
        with m_col3:
            total_cand = analysis_result.dependency_report.total_outdated if analysis_result.dependency_report else 0
            st.metric("Upgrades Available", total_cand)
        with m_col4:
            insp_files = len(analysis_result.inspection_report.inspected_files) if analysis_result.inspection_report else 0
            st.metric("Files Inspected", insp_files)
        with m_col5:
            findings_count = len(analysis_result.inspection_report.findings) if analysis_result.inspection_report else 0
            st.metric("Code Findings", findings_count)

        # 3.1 Candidate Dependency Upgrades Review
        st.subheader("1. Review Candidate Dependency Upgrades")
        dep_report = analysis_result.dependency_report
        candidate_deps = []
        if dep_report:
            for m_analysis in dep_report.manifest_analyses:
                for d in m_analysis.dependencies:
                    if d.has_candidate_upgrade and d.current_version and d.candidate_version:
                        candidate_deps.append(d)

        if candidate_deps:
            st.caption("Select which dependency upgrades to approve and apply to the repository manifests:")
            for dep in candidate_deps:
                norm_name = normalize_package_name(dep.package_name)
                col_check, col_name, col_cur, col_cand, col_prov, col_risk = st.columns([1, 3, 2, 2, 2, 3])

                with col_check:
                    st.checkbox(
                        "Approve",
                        value=True,
                        key=f"p2_dep_toggle_{norm_name}",
                        label_visibility="collapsed",
                    )
                with col_name:
                    st.markdown(f"**`{dep.package_name}`**  \n<small>`{dep.source_file}`</small>", unsafe_allow_html=True)
                with col_cur:
                    st.markdown(f"Current: `{dep.current_version}`")
                with col_cand:
                    st.markdown(f"Candidate: `{dep.candidate_version}`")
                with col_prov:
                    st.caption(f"{dep.provenance}")
                with col_risk:
                    if dep.ai_risk_assessment and not dep.ai_risk_assessment.has_error:
                        risk_badge = f"**Risk: {dep.ai_risk_assessment.risk_level}**"
                        st.markdown(f"{risk_badge}  \n<small>{dep.ai_risk_assessment.breaking_changes_summary[:80]}...</small>", unsafe_allow_html=True)
                    else:
                        st.caption("Deterministic evaluation")
                st.divider()
        else:
            st.info("No outdated dependencies detected across discovered manifests.")

        # 3.2 Source Code Inspection & Proposed AI Improvements
        st.subheader("2. Review Proposed Code Improvements")
        proposed_fixes = analysis_result.proposed_improvements

        if proposed_fixes:
            st.caption(
                "Review AI-generated code improvements synthesized for your repository. "
                "Each proposal has been pre-validated with Python AST syntax compilation. "
                "Select which improvements you approve before applying them."
            )

            for idx, prop in enumerate(proposed_fixes):
                with st.expander(f"Finding {prop.finding_id}: {prop.file_path}", expanded=True):
                    col_info, col_toggle = st.columns([5, 1])
                    with col_info:
                        st.markdown(f"**Explanation:** {prop.explanation}")
                        if prop.error_message:
                            st.warning(f"Note: {prop.error_message}")
                    with col_toggle:
                        st.checkbox(
                            "Approve Fix",
                            value=prop.status == ProposedImprovement(None, None, None, None, None, "PROPOSED").status,
                            key=f"p2_fix_toggle_{prop.finding_id}_{idx}",
                        )

                    if prop.diff:
                        st.markdown("**Proposed Unified Diff:**")
                        st.code(prop.diff, language="diff")
                    else:
                        st.code(f"# Replacement snippet:\n{prop.replacement_snippet}", language="python")
        else:
            st.info("No AI code improvements proposed for inspected files.")

        # 3.3 Apply & Validate Button (Human Approval Gate)
        st.divider()
        st.subheader("3. Apply Approved Changes & Run Sandbox Validation")
        st.caption("Clicking below applies strictly the approved modifications to the working copy and triggers isolated sandboxed testing.")

        apply_col, _ = st.columns([3, 4])
        with apply_col:
            apply_clicked = st.button(
                "Apply Approved Changes & Validate",
                type="primary",
                use_container_width=True,
                key="p2_apply_btn",
            )

        if apply_clicked:
            ws: Optional[WorkspaceSession] = st.session_state.get("p2_workspace")
            if not ws or not ws.is_active:
                st.error("Active workspace expired or cleaned up. Please click 'Analyze Repository' again.")
                return

            # Collect approved candidate upgrades
            approved_upgrades_dict: Dict[str, str] = {}
            for dep in candidate_deps:
                norm_name = normalize_package_name(dep.package_name)
                if st.session_state.get(f"p2_dep_toggle_{norm_name}", True):
                    approved_upgrades_dict[norm_name] = dep.candidate_version or ""

            # Collect approved proposed fixes
            selected_fixes: List[ProposedImprovement] = []
            for idx, prop in enumerate(proposed_fixes):
                if st.session_state.get(f"p2_fix_toggle_{prop.finding_id}_{idx}", True):
                    selected_fixes.append(prop)

            orchestrator = PipelineOrchestrator(
                ollama_client=ollama_client,
                catalog=catalog,
                sandbox_manager=sandbox_mgr,
            )

            with st.status("Applying Changes & Running Validation...", expanded=True) as status_box2:
                def on_progress2(stage: PipelineStage, status: StageStatus, message: str, details=None):
                    icon = "⏳" if status == StageStatus.RUNNING else ("✅" if status == StageStatus.COMPLETED else ("❌" if status == StageStatus.FAILED else "⏭️"))
                    status_box2.write(f"{icon} **{stage.value}**: {message}")

                final_result = orchestrator.run_improvement_and_export_stages(
                    ws=ws,
                    result=analysis_result,
                    approved_improvements=selected_fixes,
                    approved_upgrades=approved_upgrades_dict,
                    ollama_model=st.session_state.get("p2_active_model"),
                    progress_callback=on_progress2,
                )

                # Clean up workspace
                ws.cleanup()
                st.session_state["p2_workspace"] = None
                st.session_state["p2_final_result"] = final_result

                status_box2.update(label="Validation & Artifact Generation Complete!", state="complete", expanded=False)

    # --- Step 4: Final Results, Sandbox Status & Downloadable Deliverables ---
    final_result: Optional[PipelineResult] = st.session_state.get("p2_final_result")
    if final_result and final_result.export_deliverables:
        st.divider()
        st.subheader("4. Validation Results & Verified Deliverables")

        # Validation status banner
        val_rep = final_result.validation_report
        if val_rep:
            if val_rep.overall_status == ValidationStatus.PASSED:
                st.success(f"**Validation Status: PASSED** ({val_rep.passed_count}/{val_rep.total_checks} checks passed in sandbox).")
            elif val_rep.overall_status == ValidationStatus.FAILED:
                st.error(f"**Validation Status: FAILED** ({val_rep.failed_count} check(s) failed inside the sandbox).")
            elif val_rep.overall_status == ValidationStatus.NOT_RUNNABLE:
                st.warning(
                    f"**Validation Status: NOT_RUNNABLE** ({val_rep.summary_message}). "
                    "In adherence to safety protocol, untrusted repository code was not executed on the host."
                )
            else:
                st.info(f"**Validation Status:** {val_rep.overall_status.value}")

            # Expandable command logs
            with st.expander("View Sandboxed Validation Logs", expanded=False):
                for cmd_res in val_rep.command_results:
                    st.markdown(f"**Command:** `{' '.join(cmd_res.command_args)}` — **Status: {cmd_res.status.value}** (Exit Code: {cmd_res.exit_code})")
                    if cmd_res.stdout:
                        st.code(cmd_res.stdout, language="text")
                    if cmd_res.stderr:
                        st.code(cmd_res.stderr, language="text")
                    st.divider()

        # Deliverable Download Buttons
        deliverables = final_result.export_deliverables
        components = extract_repo_components(final_result.repo_url)
        repo_slug = components[1] if components else "repository"

        dl_col1, dl_col2, dl_col3 = st.columns(3)

        # 1. Patch Download
        with dl_col1:
            if deliverables.patch_result.is_valid:
                st.download_button(
                    label="Download Unified Patch (.patch)",
                    data=deliverables.patch_result.patch_content,
                    file_name=f"{repo_slug}.patch",
                    mime="text/x-diff",
                    use_container_width=True,
                )
                st.caption(f"Pre-validated via `git apply --check` ({len(deliverables.patch_result.modified_files)} modified).")
            else:
                st.error("Unified patch validation failed; download disabled.")

        # 2. ZIP Download
        with dl_col2:
            if deliverables.zip_result.is_valid and deliverables.zip_result.zip_path.exists():
                try:
                    zip_bytes = deliverables.zip_result.zip_path.read_bytes()
                    st.download_button(
                        label="Download Improved Project (.zip)",
                        data=zip_bytes,
                        file_name=f"{repo_slug}-improved.zip",
                        mime="application/zip",
                        use_container_width=True,
                    )
                    st.caption(f"Verified intact via `testzip()` ({deliverables.zip_result.file_count} files).")
                except Exception as zip_read_err:
                    st.error(f"ZIP read error: {zip_read_err}")
            else:
                st.error("ZIP archive could not be verified; download disabled.")

        # 3. Report Download
        with dl_col3:
            st.download_button(
                label="Download Analysis Report (.md)",
                data=deliverables.report_markdown,
                file_name=f"{repo_slug}-report.md",
                mime="text/markdown",
                use_container_width=True,
            )
            st.caption("Structured Markdown summary of all pipeline stages.")

        # Preview unified patch and report
        with st.expander("Preview Unified Patch", expanded=False):
            if deliverables.patch_result.patch_content:
                st.code(deliverables.patch_result.patch_content, language="diff")
            else:
                st.info("No modifications in unified patch.")

        with st.expander("Preview Full Markdown Report", expanded=False):
            st.markdown(deliverables.report_markdown)


# ===========================================================================
# TAB 2: Phase 1 - Single Manifest Review (Preserved Exactly)
# ===========================================================================

def render_phase1_single_manifest(catalog: Catalog) -> None:
    """Render Tab 2: Single Manifest Review (Original Phase 1 Workflow Preserved Verbatim)."""
    st.markdown(
        "Upload or paste your `requirements.txt` file below to identify outdated dependencies, "
        "review candidate upgrades from the local dataset, and preview unified diffs before accepting changes."
    )

    # Provenance Banner
    st.warning(
        "**Local Catalog Prototype Mode:** Proposed candidate versions originate from a local dataset. "
        "They are **not verified against live PyPI**, and the tool will not install packages or modify your repository."
    )

    # --- Step 1: Ingestion (Upload or Paste) ---
    st.subheader("1. Input Requirements Manifest")

    input_mode = st.radio(
        "Choose input method:",
        options=["Direct Text / Editor", "Upload requirements.txt"],
        horizontal=True,
    )

    raw_input_text = ""

    if input_mode == "Upload requirements.txt":
        uploaded_file = st.file_uploader(
            "Upload a plain text requirements file (max 1 MB)",
            type=["txt"],
            help="Select your requirements.txt or equivalent text manifest.",
        )
        if uploaded_file is not None:
            if uploaded_file.size > MAX_FILE_SIZE_BYTES:
                st.error("Uploaded file exceeds maximum limit of 1 MB. Please provide a smaller file.")
                return
            try:
                raw_input_text = uploaded_file.getvalue().decode("utf-8")
            except UnicodeDecodeError:
                st.error("Unable to decode the uploaded file as UTF-8 plain text. Please verify the file encoding.")
                return
    else:
        initial_val = st.session_state.get("raw_content", "")
        raw_input_text = st.text_area(
            "Paste requirements.txt content:",
            value=initial_val,
            height=180,
            placeholder="e.g.:\nrequests==2.28.0\nurllib3==1.26.15\n# comments and other options...",
        )

    if not raw_input_text.strip():
        st.info("Upload a file or paste `requirements.txt` content above to begin inspection.")
        return

    # --- Step 2: Parsing & Evaluation ---
    lines = parse_requirements_content(raw_input_text)
    evaluations = compare_requirements(lines, catalog)

    upgrade_candidates = [e for e in evaluations if e.is_upgrade_candidate]
    up_to_date_count = sum(1 for e in evaluations if e.status == UpgradeStatus.UP_TO_DATE)
    unknown_count = sum(1 for e in evaluations if e.status == UpgradeStatus.UNKNOWN_PACKAGE)
    preserved_count = sum(
        1 for e in evaluations if e.status in (UpgradeStatus.PRESERVED_NON_PIN, UpgradeStatus.PRESERVED_PASSTHROUGH)
    )

    # Summary Metrics Cards
    st.divider()
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Lines", len(lines))
    with col2:
        st.metric("Upgrades Available", len(upgrade_candidates))
    with col3:
        st.metric("Up to Date", up_to_date_count)
    with col4:
        st.metric("Preserved / Other", preserved_count + unknown_count)

    # --- Step 3: Interactive Review Table & Selection ---
    st.subheader("2. Review Candidate Upgrades & Select Changes")
    st.caption(
        "Inspect each dependency line below. Checkboxes allow you to selectively accept "
        "or reject specific upgrades. Unchecked packages and non-pinned requirements will remain unchanged."
    )

    if not upgrade_candidates:
        st.success("No outdated exact-pinned dependencies detected! All recognized pinned packages are up to date.")

    # Selection controls
    accepted_line_numbers: Set[int] = set()

    # Pre-populate session state defaults for candidate toggles if not yet initialized
    content_hash = hash(raw_input_text)
    if st.session_state.get("last_content_hash") != content_hash:
        st.session_state["last_content_hash"] = content_hash
        for cand in upgrade_candidates:
            st.session_state[f"upgrade_toggle_{cand.line.line_number}"] = True

    # Controls to Select All / Deselect All
    if upgrade_candidates:
        btn_col1, btn_col2, _ = st.columns([1, 1, 4])
        with btn_col1:
            if st.button("Select All", use_container_width=True):
                for cand in upgrade_candidates:
                    st.session_state[f"upgrade_toggle_{cand.line.line_number}"] = True
                st.rerun()
        with btn_col2:
            if st.button("Deselect All", use_container_width=True):
                for cand in upgrade_candidates:
                    st.session_state[f"upgrade_toggle_{cand.line.line_number}"] = False
                st.rerun()

    # Table rendering
    for eval_item in evaluations:
        line_num = eval_item.line.line_number
        status = eval_item.status

        # If it's an upgrade candidate, render interactive row with checkbox
        if eval_item.is_upgrade_candidate:
            is_checked = st.session_state.get(f"upgrade_toggle_{line_num}", True)
            col_check, col_pkg, col_curr, col_cand, col_status = st.columns([1, 3, 2, 2, 4])

            with col_check:
                toggled = st.checkbox(
                    "Accept",
                    value=is_checked,
                    key=f"upgrade_toggle_{line_num}",
                    label_visibility="collapsed",
                )
                if toggled:
                    accepted_line_numbers.add(line_num)

            with col_pkg:
                st.markdown(f"**`{eval_item.display_package_name}`** *(Line {line_num})*")
            with col_curr:
                st.markdown(f"Current: `{eval_item.current_version}`")
            with col_cand:
                st.markdown(f"Proposed: `{eval_item.candidate_version}`")
            with col_status:
                badge = "🟢 Upgrade Available"
                st.markdown(f"{badge}  \n<small>{eval_item.status_message}</small>", unsafe_allow_html=True)
            st.divider()

    # Expandable detail section for Up to Date, Non-Pinned, and Preserved lines
    with st.expander("View Up-to-Date, Preserved, and Non-Pinned Lines", expanded=False):
        for eval_item in evaluations:
            if not eval_item.is_upgrade_candidate:
                line_num = eval_item.line.line_number
                status_label = eval_item.status.value
                line_text = eval_item.line.raw_text.strip() or "(empty line)"

                col_num, col_raw, col_stat = st.columns([1, 6, 4])
                with col_num:
                    st.caption(f"Line {line_num}")
                with col_raw:
                    st.code(line_text, language="text")
                with col_stat:
                    st.caption(f"Status: **{status_label}**  \n{eval_item.status_message}")

    # --- Step 4: Proposed Output & Unified Diff ---
    st.subheader("3. Unified Code Diff & Proposed File")

    updated_requirements_text = synthesize_updated_requirements(evaluations, accepted_line_numbers)
    diff_text = generate_unified_diff(raw_input_text, updated_requirements_text)

    diff_col, code_col = st.columns(2)

    with diff_col:
        st.markdown("**Unified Diff (`git diff` style):**")
        if diff_text:
            st.code(diff_text, language="diff")
        else:
            st.info("No modifications selected. Original and proposed content are identical.")

    with code_col:
        st.markdown("**Proposed `requirements.txt` Content:**")
        st.code(updated_requirements_text, language="text")

    # --- Step 5: Export & Action Gate ---
    st.divider()
    st.subheader("4. Accept Changes & Download")

    if accepted_line_numbers:
        st.success(
            f"**{len(accepted_line_numbers)} upgrade(s) accepted.** "
            "You can now download the updated manifest and test it in your local repository."
        )
    else:
        st.info(" No upgrades are currently selected for export.")

    btn_download_col, _ = st.columns([2, 5])
    with btn_download_col:
        st.download_button(
            label="Download Proposed requirements.txt",
            data=updated_requirements_text,
            file_name="requirements-updated.txt",
            mime="text/plain",
            use_container_width=True,
            disabled=(len(accepted_line_numbers) == 0 and diff_text == ""),
        )


# ===========================================================================
# Application Entry Point
# ===========================================================================

def main() -> None:
    catalog = get_catalog()

    # --- Sidebar: Information & Samples ---
    st.sidebar.title("📦 Upgrade Assistant")
    st.sidebar.markdown(
        "A lightweight assistant helping open-source contributors inspect, "
        "select, and validate Python dependency upgrades before committing."
    )

    st.sidebar.divider()
    st.sidebar.subheader("Provenance Notice")
    st.sidebar.info(
        f"**Catalog Version:** {catalog.metadata.catalog_version}  \n"
        f"**Catalog Date:** {catalog.metadata.catalog_timestamp}  \n"
        f"**Tracked Packages:** {len(catalog)}  \n\n"
        f"*{catalog.metadata.provenance_notice}*"
    )

    st.sidebar.divider()
    st.sidebar.subheader("Load Sample Files")
    st.sidebar.caption("Test the workflow with pre-built sample files:")

    if st.sidebar.button("Load Outdated Sample", use_container_width=True):
        st.session_state["raw_content"] = load_sample_file("requirements-outdated.txt")
        st.session_state["active_sample"] = "requirements-outdated.txt"

    if st.sidebar.button("Load Clean (Up-to-Date) Sample", use_container_width=True):
        st.session_state["raw_content"] = load_sample_file("requirements-clean.txt")
        st.session_state["active_sample"] = "requirements-clean.txt"

    if st.sidebar.button("Load Complex Sample (Markers/Comments)", use_container_width=True):
        st.session_state["raw_content"] = load_sample_file("requirements-complex.txt")
        st.session_state["active_sample"] = "requirements-complex.txt"

    st.sidebar.divider()
    st.sidebar.caption("AI-DLC Prototype • Human-in-the-Loop")

    # --- Main Screen Header ---
    st.title("Dependency Upgrade Assistant")

    # Dual-Tab Navigation
    tab_repo, tab_manifest = st.tabs([
        "Repository Analysis & Improvement",
        "Single Manifest Review",
    ])

    with tab_repo:
        render_phase2_repository_tab(catalog)

    with tab_manifest:
        render_phase1_single_manifest(catalog)


if __name__ == "__main__":
    main()
