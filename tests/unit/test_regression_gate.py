"""Release regression gate (audit 2026-09-11 §11 Wave 0 item 4).

Each row names a correctness fix that must never silently lose its proof: the commit that landed
it, what it protects, and the test functions that prove it. Renaming or deleting one of those
tests turns this file red -- the gate is the list, not a marker someone can forget to apply.
Adding a gated fix means adding a row (and, if it changes recorded bytes or a CLI contract, a
MINOR bump per CHANGELOG.md). Commits are labels for humans; nothing here reads git.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

GATE: list[tuple[str, str, dict[str, list[str]]]] = [
    (
        "8c6b5c4",
        "backup: ledgers are pushed as the manifest's snapshot; manifest paths cannot leave a "
        "root; external files are restored only into a directory that already exists",
        {
            "tests/unit/backup/test_dest_push.py": [
                "test_push_sends_the_snapshot_of_a_grown_ledger",
            ],
            "tests/unit/backup/test_pull_status.py": [
                "test_pull_external_only_into_an_existing_directory",
            ],
            "tests/unit/backup/test_schema.py": [
                "test_entry_rejects_paths_that_can_leave_the_root",
                "test_external_entry_needs_an_absolute_source",
            ],
        },
    ),
    (
        "883da62",
        "backup: end to end on a local vault and a fake rclone that leaks a secret on every "
        "call; --forget-remote only after the whole manifest verified; privacy scan zero hits",
        {
            "tests/unit/test_e2e_backup.py": ["test_local_vault_story", "test_fake_rclone_story"],
            "tests/unit/backup/test_dest_push.py": [
                "test_forget_remote_only_after_everything_verified",
            ],
        },
    ),
    (
        "d9b2331",
        "cli: a failing command still names its dataset / run / recipe / id in the VERDICT",
        {
            "tests/unit/test_cli_eval.py": ["test_eval_failure_preserves_command_identity"],
            "tests/unit/test_cli_fuse.py": ["test_fuse_failure_preserves_command_identity"],
            "tests/unit/test_cli_submit.py": ["test_submit_failure_preserves_command_identity"],
            "tests/unit/test_cli_train.py": ["test_train_failure_preserves_command_identity"],
        },
    ),
    (
        "7c31c3d",
        "fuse: a run whose fuse.json is lost is refused unless --replace rebuilds every subset "
        "the card declares (a partial record would be collected as evidence)",
        {
            "tests/unit/fuse/test_build.py": [
                "test_missing_record_cache_refusal_is_read_only",
                "test_missing_record_replace_rebuilds_subsets_omitted_from_request",
                "test_missing_record_replace_checks_omitted_member_before_writes",
            ],
        },
    ),
    (
        "12a9cd4",
        "submit: uploaded / scored / foreign rows come from one normalised event source; upload "
        "checks run in the spec's order so a compound failure reports the right reason",
        {
            "tests/unit/submit/test_actions.py": [
                "test_upload_checks_profile_and_guards_before_artifact_hash",
            ],
            "tests/unit/submit/test_schema.py": [
                "test_event_requirements_cover_exactly_the_event_type",
            ],
        },
    ),
    (
        "6a4cc58",
        "submit: duplicate CSV headers are refused before writing; nested fusion pairing names "
        "the differing leaf; the latest judgement of a prereg decides admission",
        {
            "tests/unit/submit/test_gate.py": ["test_same_prereg_uses_its_latest_judgement"],
            "tests/unit/submit/test_pairing.py": [
                "test_nested_fusion_pairing_reports_the_differing_leaf",
            ],
            "tests/unit/submit/test_writer_csv_boxes.py": [
                "test_duplicate_headers_are_rejected_before_writing",
                "test_norm_rejects_a_box_with_an_out_of_range_view",
            ],
            "tests/unit/submit/test_writer_scores_csv.py": [
                "test_duplicate_headers_are_rejected_before_writing",
            ],
            "tests/unit/test_cli_submit.py": [
                "test_stage_rejects_duplicate_columns_without_artifacts",
                "test_stage_allow_missing_warns_with_count",
                "test_verify_fusion_checks_output_but_does_not_rehash_members",
            ],
        },
    ),
    (
        "d193113 (VCP-008)",
        "measure: a pre-registration is trusted only while its bytes hash to its FIRST log row; "
        "a yaml without a row was never registered",
        {
            "tests/unit/measure/test_prereg_judge.py": [
                "test_prereg_tampering_differs_from_first_logged_sha",
                "test_prereg_yaml_without_a_log_row_was_never_registered",
            ],
        },
    ),
    (
        "d881a1d (VCP-009)",
        "submit: a known foreign ref refreshes its status / score as a new snapshot without a "
        "second arrival; quota counts each ref once; re-syncing the same page appends nothing",
        {
            "tests/unit/submit/test_ledger.py": [
                "test_arrivals_use_the_latest_snapshot_of_a_foreign_ref",
            ],
            "tests/unit/submit/test_sync.py": [
                "test_sync_refreshes_pending_foreign_score_without_counting_a_second_arrival",
                "test_foreign_pending_to_error_is_a_snapshot_without_a_score",
                "test_foreign_complete_score_correction_keeps_the_newest",
                "test_foreign_without_a_platform_ref_still_refreshes_by_its_derived_ref",
                "test_foreign_duplicate_rows_at_the_same_time",
            ],
        },
    ),
    (
        "wave 1a (VCP-005, VCP-007)",
        "artifact: an id is claimed at open and never rewritten; a crash before commit leaves no "
        "manifest; a supersedes target must exist, be committed and verify; clean never reaches "
        "a committed artifact; an id_pattern group must equal its spec field",
        {
            "tests/unit/artifact/test_writer.py": [
                "test_open_claims_the_id_and_writes_spec_json",
                "test_manifest_publish_failure_leaves_a_partial",
            ],
            "tests/unit/artifact/test_lineage.py": [
                "test_supersession_is_checked_at_open_and_commit_and_indexed",
            ],
            "tests/unit/artifact/test_clean.py": [
                "test_clean_lists_then_removes_only_old_partials_and_temps",
                "test_clean_never_touches_what_it_cannot_read_or_what_committed_meanwhile",
            ],
            "tests/unit/artifact/test_schema.py": ["test_id_pattern_groups_must_equal_the_fields"],
            "tests/unit/core/test_atomic.py": [
                "test_second_write_is_refused_and_the_original_is_untouched",
                "test_the_four_write_once_sites_use_the_primitive",
            ],
            "tests/unit/test_e2e_artifact.py": ["test_selection_job_story"],
        },
    ),
    (
        "wave 1b-1 (VCP-001, VCP-003)",
        "access: rows outside the allowed subsets are never parsed; a denied read fails closed "
        "and is counted; the receipt is the accessor's, not the caller's; a run that read a "
        "subset loses it as a clean base in measure and judge; a profile can require receipts",
        {
            "tests/unit/data/test_access.py": [
                "test_train_only_access_never_parses_other_rows",
                "test_unauthorized_subsets_fail_closed_and_are_counted",
                "test_receipt_is_written_even_when_the_job_fails",
            ],
            "tests/unit/train/test_run.py": [
                "test_train_run_binds_the_childs_receipts_and_warns_on_observed_beyond",
            ],
            "tests/unit/measure/test_measure.py": ["test_observed_subsets_are_not_clean_bases"],
            "tests/unit/measure/test_prereg_judge.py": [
                "test_a_run_that_read_a_claimed_subset_makes_the_judgement_invalid",
            ],
            "tests/unit/submit/test_stage.py": [
                "test_stage_records_provenance_and_the_profile_can_require_it",
            ],
            "tests/unit/test_e2e_access.py": [
                "test_training_receipts_grade_measure_and_judge",
                "test_a_profile_can_require_receipts_at_the_gate",
            ],
        },
    ),
    (
        "wave 1b-2 (VCP-002)",
        "source audit: an audited open never hashes the whole file; a tampered audit or a "
        "tampered selected row fails closed while an unselected row is never read; audited and "
        "full-hash reads are equivalent; consumers record identity and warn without an audit",
        {
            "tests/unit/data/test_source_audit.py": [
                "test_write_source_audit_creates_then_reuses",
                "test_load_source_audit_fails_closed_on_tampering",
            ],
            "tests/unit/data/test_access.py": [
                "test_an_audited_open_never_hashes_the_whole_file",
                "test_a_tampered_selected_row_is_refused_and_an_unselected_one_is_not",
                "test_audited_and_full_hash_reads_are_equivalent",
            ],
            "tests/unit/train/test_run.py": [
                "test_train_run_warns_when_the_dataset_has_no_source_audit",
            ],
            "tests/unit/test_e2e_source_audit.py": ["test_source_audit_flow"],
        },
    ),
    (
        "VCP-035/039 (#24)",
        "backup / train: a checkpoint is its path -- same-named checkpoints in different folders "
        "all reach the manifest and verify, and upload under folder-qualified remote names",
        {
            "tests/unit/backup/test_evidence_run.py": [
                "test_same_named_checkpoints_in_different_folders_are_all_collected",
                "test_identical_folds_keep_their_own_remote_copy",
            ],
            "tests/unit/backup/test_verify.py": ["test_verify_checks_every_same_named_checkpoint"],
            "tests/unit/train/test_upload.py": [
                "test_same_named_checkpoints_upload_under_folder_qualified_names",
                "test_remote_names_use_the_shortest_distinguishing_folders",
            ],
            "tests/unit/train/test_status.py": [
                "test_five_folds_named_model_pt_all_upload_and_leave_nothing_unbacked",
            ],
        },
    ),
    (
        "VCP-036 (#25)",
        "submit: a kernel candidate may load only the judged weights, and every weights run "
        "passes the sealed and provenance checks; final reads a kernel submission the same way",
        {
            "tests/unit/submit/test_kernel.py": [
                "test_a_candidate_may_load_only_the_judged_weights",
                "test_every_candidate_weights_run_passes_the_sealed_checks",
                "test_a_probe_is_exempt_but_every_finding_is_written_down",
                "test_kernel_provenance_folds_every_weights_run_in",
            ],
            "tests/unit/submit/test_stage.py": [
                "test_kernel_candidate_cannot_carry_weights_that_were_not_judged",
            ],
            "tests/unit/submit/test_final.py": [
                "test_final_reads_a_kernel_submission_the_way_stage_recorded_it",
            ],
        },
    ),
    (
        "VCP-037 (#27)",
        "submit: a Kaggle upload the CLI does not confirm is read back (description opens with "
        "the id, inside the CLI's run give or take two minutes); nothing in the read-back fails "
        "the upload; 'Could not submit' at exit 0 writes no row; the VERDICT carries the reply",
        {
            "tests/unit/submit/test_platforms.py": [
                "test_kaggle_kernel_upload_confirms_by_reading_the_submission_back",
                "test_the_window_opens_before_the_cli_started_and_closes_after_it_returned",
                "test_a_read_back_cut_short_still_returns_the_upload",
                "test_a_file_the_cli_could_not_submit_fails_although_the_cli_exited_0",
            ],
            "tests/unit/submit/test_actions.py": [
                "test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing",
            ],
            "tests/unit/test_cli_submit.py": [
                "test_upload_verdict_carries_the_platform_ref_and_detail_into_the_log",
            ],
        },
    ),
    (
        "VCP-038 (#28)",
        "submit: an upload another ledger wrote as foreign is one arrival once a ref ties it to "
        "the id (closest first, within ten minutes); a same-score re-upload gets its own scored "
        "row, so its ref ties",
        {
            "tests/unit/submit/test_ledger.py": [
                "test_a_foreign_row_for_our_own_upload_is_one_arrival",
                "test_an_upload_absorbs_its_own_twin_before_a_nearby_web_upload",
                "test_a_tied_ref_far_from_every_upload_still_counts",
            ],
            "tests/unit/submit/test_sync.py": [
                "test_a_re_upload_that_scores_the_same_still_gets_its_own_scored_row",
                "test_a_foreign_row_the_ledger_later_learns_is_ours_stops_counting",
            ],
            "tests/unit/submit/test_guards.py": [
                "test_quota_counts_an_upload_the_platform_also_listed_as_foreign_once",
            ],
        },
    ),
    (
        "VCP-043 (#26)",
        "train: the child learns its attempt number (VCP_ATTEMPT, Session.attempt) and it agrees "
        "with its receipt ids, after --resume and after a crash too",
        {
            "tests/unit/train/test_run.py": [
                "test_the_child_knows_its_attempt_and_its_receipts_agree",
            ],
            "tests/unit/train/test_session.py": [
                "test_attempt_is_the_exported_one_or_else_the_records",
            ],
        },
    ),
]


def _top_level_tests(rel: str) -> set[str]:
    """Names of ``test_*`` functions defined at module level (parsed, not imported: a gate must
    not depend on the test module's own imports succeeding)."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    return {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    }


@pytest.mark.parametrize("commit, protects, files", GATE, ids=[row[0] for row in GATE])
def test_gated_fix_still_has_every_proof(commit, protects, files):
    for rel, names in files.items():
        assert (ROOT / rel).is_file(), f"{commit}: {rel} is gone ({protects})"
        present = _top_level_tests(rel)
        missing = [n for n in names if n not in present]
        assert not missing, f"{commit}: {rel} lost {missing} -- it proved: {protects}"


def test_gate_rows_are_unique_and_non_empty():
    commits = [row[0] for row in GATE]
    assert len(set(commits)) == len(commits)
    assert all(row[2] and all(row[2].values()) for row in GATE)
