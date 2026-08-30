#!/bin/sh

expected_name='Auremgrid'
expected_email='auremgrid@users.noreply.github.com'

require_expected_identity() {
  identity_kind="$1"
  case "$identity_kind" in
    AUTHOR) identity_label='author' ;;
    COMMITTER) identity_label='committer' ;;
    *) identity_label="$identity_kind" ;;
  esac
  identity_value="$(git var "GIT_${identity_kind}_IDENT" 2>/dev/null)" || {
    echo "Unable to read Git $identity_label identity." >&2
    exit 1
  }

  case "$identity_value" in
    "$expected_name <$expected_email>"*) ;;
    *)
      echo "Blocked: $identity_label identity must be $expected_name <$expected_email>." >&2
      exit 1
      ;;
  esac
}

require_expected_identities() {
  require_expected_identity AUTHOR
  require_expected_identity COMMITTER
}

reject_attribution_trailers() {
  message_file="$1"
  if grep -Eiq '^[[:space:]]*(Co-authored-by|Signed-off-by|Reviewed-by|Acked-by|Tested-by|Suggested-by|Generated-by|Assisted-by|Pair-programmed-by)[[:space:]]*:' "$message_file"; then
    echo 'Blocked: commit messages may not include third-party attribution trailers.' >&2
    exit 1
  fi
}

reject_staged_reserved_attribution() {
  reserved_part_one='co'
  reserved_part_two='dex'
  reserved_word="${reserved_part_one}${reserved_part_two}"

  if git diff --cached --no-ext-diff -U0 | grep -E '^\+[^+]' | grep -Fqi "$reserved_word"; then
    echo 'Blocked: staged content includes a reserved third-party attribution reference.' >&2
    exit 1
  fi
}
