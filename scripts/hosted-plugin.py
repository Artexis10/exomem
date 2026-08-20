#!/usr/bin/env python
"""Render, verify, archive, and maintain Hosted client plugin candidates."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from exomem import hosted_plugins  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "render",
            "regenerate",
            "check",
            "archive",
            "promote",
            "demote",
            "status",
            "directory-check",
            "directory-render",
            "directory-status",
            "directory-record",
            "directory-activate",
        ),
    )
    parser.add_argument("--openai-app-id")
    parser.add_argument(
        "--candidate",
        choices=tuple(hosted_plugins.CANDIDATE_PROFILES),
        default=hosted_plugins.DEFAULT_CANDIDATE,
    )
    parser.add_argument("--platform", choices=(*hosted_plugins.PLATFORMS, "all"))
    parser.add_argument("--channel", choices=(*hosted_plugins.DIRECTORY_CHANNELS, "all"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--deployment-sha256")
    parser.add_argument("--listing-version")
    parser.add_argument("--target-submission-sha256")
    parser.add_argument("--expected-active-submission-sha256")
    parser.add_argument("--directory-state", choices=tuple(hosted_plugins.DIRECTORY_STATES))
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--records-expectation", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--operator-key-id")
    parser.add_argument(
        "--expected-state",
        choices=("pending", "live", "failed", *hosted_plugins.DIRECTORY_STATES),
    )
    parser.add_argument("--expected-record-sha256")
    args = parser.parse_args()
    try:
        if args.command == "directory-check":
            packets = hosted_plugins.directory_check(
                REPO_ROOT,
                channel=args.channel or "all",
                openai_app_id=args.openai_app_id,
            )
            print(
                json.dumps(
                    packets if args.channel in (None, "all") else packets[args.channel],
                    sort_keys=True,
                )
            )
        elif args.command == "directory-render":
            print(
                hosted_plugins.directory_render(
                    REPO_ROOT,
                    args.output,
                    channel=args.channel or "all",
                    openai_app_id=args.openai_app_id,
                )
            )
        elif args.command == "directory-status":
            print(
                json.dumps(
                    hosted_plugins.directory_status(
                        REPO_ROOT,
                        openai_app_id=args.openai_app_id,
                        trusted_key_id=args.operator_key_id
                        or os.environ.get("EXOMEM_HOSTED_PROMOTION_KEY_ID"),
                        trusted_secret=os.environ.get("EXOMEM_HOSTED_PROMOTION_SECRET"),
                        deployment_sha256=args.deployment_sha256
                        or os.environ.get("EXOMEM_HOSTED_DEPLOYMENT_SHA256"),
                    ),
                    sort_keys=True,
                )
            )
        elif args.command == "directory-record":
            if (
                args.channel not in hosted_plugins.DIRECTORY_CHANNELS
                or not args.directory_state
                or not args.expected_state
                or not args.expected_record_sha256
            ):
                parser.error(
                    "directory-record requires --channel, --directory-state, --expected-state, and --expected-record-sha256"
                )
            receipt = json.loads(args.receipt.read_text(encoding="utf-8")) if args.receipt else None
            expected_active = (
                None
                if args.expected_active_submission_sha256 == "none"
                else args.expected_active_submission_sha256
            )
            hosted_plugins.record_directory_state(
                REPO_ROOT,
                args.channel,
                args.directory_state,
                expected_state=args.expected_state,
                expected_record_sha256=args.expected_record_sha256,
                receipt=receipt,
                openai_app_id=args.openai_app_id,
                trusted_key_id=args.operator_key_id
                or os.environ.get("EXOMEM_HOSTED_PROMOTION_KEY_ID"),
                trusted_secret=os.environ.get("EXOMEM_HOSTED_PROMOTION_SECRET"),
                deployment_sha256=args.deployment_sha256
                or os.environ.get("EXOMEM_HOSTED_DEPLOYMENT_SHA256"),
                listing_version=args.listing_version,
                target_submission_sha256=args.target_submission_sha256,
                expected_active_submission_sha256=expected_active,
            )
        elif args.command == "directory-activate":
            if (
                args.channel not in hosted_plugins.DIRECTORY_CHANNELS
                or not args.target_submission_sha256
                or args.expected_active_submission_sha256 is None
            ):
                parser.error(
                    "directory-activate requires --channel, --target-submission-sha256, and --expected-active-submission-sha256 (or none)"
                )
            expected_active = (
                None
                if args.expected_active_submission_sha256 == "none"
                else args.expected_active_submission_sha256
            )
            hosted_plugins.activate_directory_submission(
                REPO_ROOT,
                args.channel,
                target_submission_sha256=args.target_submission_sha256,
                expected_active_submission_sha256=expected_active,
                openai_app_id=args.openai_app_id,
                trusted_key_id=args.operator_key_id
                or os.environ.get("EXOMEM_HOSTED_PROMOTION_KEY_ID"),
                trusted_secret=os.environ.get("EXOMEM_HOSTED_PROMOTION_SECRET"),
                deployment_sha256=args.deployment_sha256
                or os.environ.get("EXOMEM_HOSTED_DEPLOYMENT_SHA256"),
            )
        elif args.command == "render":
            print(
                hosted_plugins.render(
                    REPO_ROOT,
                    openai_app_id=args.openai_app_id,
                    platform=args.platform or "claude",
                    candidate=args.candidate,
                )
            )
        elif args.command == "regenerate":
            if args.platform not in (None, "claude"):
                parser.error("regenerate supports only the committed Claude candidate")
            print(hosted_plugins.regenerate_claude(REPO_ROOT))
        elif args.command == "check":
            hosted_plugins.check(
                REPO_ROOT,
                openai_app_id=args.openai_app_id,
                platform=args.platform or "claude",
                candidate=args.candidate,
            )
            print("Hosted generated artifacts are current")
        elif args.command == "archive":
            print(
                hosted_plugins.archive(
                    REPO_ROOT,
                    openai_app_id=args.openai_app_id,
                    platform=args.platform or "claude",
                    candidate=args.candidate,
                )
            )
        elif args.command == "status":
            if args.candidate == hosted_plugins.DEFAULT_CANDIDATE:
                hosted_plugins.check_compatibility_descriptor(REPO_ROOT)
            records_expectation = (
                json.loads(args.records_expectation.read_text(encoding="utf-8"))
                if args.records_expectation
                else None
            )
            records = {
                platform: json.loads(
                    hosted_plugins.promotion_record(
                        REPO_ROOT, platform, candidate=args.candidate
                    ).read_text(encoding="utf-8")
                )
                for platform in hosted_plugins.PLATFORMS
            }
            print(
                json.dumps(
                    {
                        "distribution": hosted_plugins.distribution_manifest(
                            REPO_ROOT,
                            trusted_key_id=args.operator_key_id
                            or os.environ.get("EXOMEM_HOSTED_PROMOTION_KEY_ID"),
                            trusted_secret=os.environ.get("EXOMEM_HOSTED_PROMOTION_SECRET"),
                            candidate=args.candidate,
                            records_expectation=records_expectation,
                        ),
                        "records": {
                            platform: hosted_plugins.promotion_record_sha256(
                                REPO_ROOT, platform, candidate=args.candidate
                            )
                            for platform in records
                        },
                        "oauth_client_config_sha256": {
                            platform: (
                                record["evidence"].get("oauth_client_config_sha256")
                                if isinstance(record.get("evidence"), dict)
                                else None
                            )
                            for platform, record in records.items()
                        },
                        "registered_app_id_sha256": {
                            platform: (
                                record["evidence"].get("registered_app_id_sha256")
                                if isinstance(record.get("evidence"), dict)
                                else None
                            )
                            for platform, record in records.items()
                        },
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "promote":
            if args.platform not in hosted_plugins.PLATFORMS or not args.evidence:
                parser.error("promote requires --platform and --evidence")
            records_expectation = (
                json.loads(args.records_expectation.read_text(encoding="utf-8"))
                if args.records_expectation
                else None
            )
            hosted_plugins.promote(
                REPO_ROOT,
                args.platform,
                json.loads(args.evidence.read_text(encoding="utf-8")),
                trusted_key_id=args.operator_key_id,
                trusted_secret=os.environ.get("EXOMEM_HOSTED_PROMOTION_SECRET"),
                expected_state=args.expected_state,
                expected_record_sha256=args.expected_record_sha256,
                candidate=args.candidate,
                records_expectation=records_expectation,
            )
        else:
            if args.platform not in hosted_plugins.PLATFORMS or not args.reason:
                parser.error("demote requires --platform and --reason")
            hosted_plugins.demote(
                REPO_ROOT,
                args.platform,
                args.reason,
                expected_state=args.expected_state,
                expected_record_sha256=args.expected_record_sha256,
                candidate=args.candidate,
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
