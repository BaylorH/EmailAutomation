#!/usr/bin/env bash

PROCESS_USER_APPROVED_ACCOUNT="bp21harrison@gmail.com"
PROCESS_USER_PROJECT="email-automation-cache"
PROCESS_USER_PROJECT_NUMBER="248289505828"

process_user_gcloud_preflight() {
  local mode="${1:-apply}"
  local configured_impersonation auth_accounts auth_count project_info
  local actual_project_number lifecycle_state

  if [[ "${GCLOUD_ACCOUNT:-}" != "$PROCESS_USER_APPROVED_ACCOUNT" ]]; then
    printf 'Refusing: GCLOUD_ACCOUNT must be exactly %s.\n' \
      "$PROCESS_USER_APPROVED_ACCOUNT" >&2
    return 65
  fi
  export CLOUDSDK_CORE_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"

  if [[ -n "${CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT:-}" ]]; then
    printf 'Refusing: gcloud service-account impersonation must be disabled.\n' >&2
    return 66
  fi

  if [[ "$mode" == "dry-run" || "$mode" == "local" ]]; then
    return 0
  fi
  if [[ "$mode" != "apply" ]]; then
    printf 'Refusing: unsupported preflight mode %q.\n' "$mode" >&2
    return 64
  fi

  configured_impersonation="$(
    gcloud config get-value auth/impersonate_service_account \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT"
  )"
  if [[ -n "$configured_impersonation" && "$configured_impersonation" != "(unset)" ]]; then
    printf 'Refusing: gcloud auth/impersonate_service_account must be unset.\n' >&2
    return 69
  fi

  auth_accounts="$(
    gcloud auth list \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT" \
      "--filter=account=${PROCESS_USER_APPROVED_ACCOUNT}" \
      "--format=value(account)"
  )"
  auth_count="$(printf '%s\n' "$auth_accounts" | awk 'NF { count++ } END { print count + 0 }')"
  if [[ "$auth_count" != "1" || "$auth_accounts" != "$PROCESS_USER_APPROVED_ACCOUNT" ]]; then
    printf 'Refusing: expected exactly one gcloud auth account matching %s.\n' \
      "$PROCESS_USER_APPROVED_ACCOUNT" >&2
    return 70
  fi

  project_info="$(
    gcloud projects describe "$PROCESS_USER_PROJECT" \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT" \
      "--format=value(projectNumber,lifecycleState)"
  )"
  IFS=$'\t' read -r actual_project_number lifecycle_state <<< "$project_info"
  if [[ "$actual_project_number" != "$PROCESS_USER_PROJECT_NUMBER" || "$lifecycle_state" != "ACTIVE" ]]; then
    printf 'Refusing: expected project %s number %s ACTIVE; got %s.\n' \
      "$PROCESS_USER_PROJECT" "$PROCESS_USER_PROJECT_NUMBER" "$project_info" >&2
    return 71
  fi
}
