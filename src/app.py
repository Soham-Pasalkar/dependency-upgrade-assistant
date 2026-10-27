"""Streamlit Web Application: Dependency Upgrade Assistant."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Set

import streamlit as st

from src.core.comparator import compare_requirements
from src.core.diff_generator import generate_unified_diff, synthesize_updated_requirements
from src.core.models import UpgradeStatus
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog, load_catalog

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


def load_sample_file(filename: str) -> str:
    """Load content of a sample file from sample-data directory."""
    path = SAMPLE_DATA_DIR / filename
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def main() -> None:
    catalog = get_catalog()

    # --- Sidebar: Information & Samples ---
    st.sidebar.title("📦 Upgrade Assistant")
    st.sidebar.markdown(
        "A lightweight assistant helping open-source contributors inspect, "
        "select, and validate Python dependency upgrades before committing."
    )

    st.sidebar.divider()
    st.sidebar.subheader("📋 Provenance Notice")
    st.sidebar.info(
        f"**Catalog Version:** {catalog.metadata.catalog_version}  \n"
        f"**Catalog Date:** {catalog.metadata.catalog_timestamp}  \n"
        f"**Tracked Packages:** {len(catalog)}  \n\n"
        f"⚠️ *{catalog.metadata.provenance_notice}*"
    )

    st.sidebar.divider()
    st.sidebar.subheader("🧪 Load Sample Files")
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
    st.title("📦 Dependency Upgrade Assistant")
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
        st.info("👋 Upload a file or paste `requirements.txt` content above to begin inspection.")
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
        st.success("🎉 No outdated exact-pinned dependencies detected! All recognized pinned packages are up to date.")

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
    with st.expander("ℹ️ View Up-to-Date, Preserved, and Non-Pinned Lines", expanded=False):
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
            f"✅ **{len(accepted_line_numbers)} upgrade(s) accepted.** "
            "You can now download the updated manifest and test it in your local repository."
        )
    else:
        st.info("ℹ️ No upgrades are currently selected for export.")

    btn_download_col, _ = st.columns([2, 5])
    with btn_download_col:
        st.download_button(
            label="⬇️ Download Proposed requirements.txt",
            data=updated_requirements_text,
            file_name="requirements-updated.txt",
            mime="text/plain",
            use_container_width=True,
            disabled=(len(accepted_line_numbers) == 0 and diff_text == ""),
        )


if __name__ == "__main__":
    main()
