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
  while IFS= read -r trailer_line || [ -n "$trailer_line" ]; do
    trailer_line="${trailer_line#"${trailer_line%%[![:space:]]*}"}"
    case "$trailer_line" in
      [Cc][Oo]-[Aa][Uu][Tt][Hh][Oo][Rr][Ee][Dd]-[Bb][Yy]:*|\
      [Ss][Ii][Gg][Nn][Ee][Dd]-[Oo][Ff][Ff]-[Bb][Yy]:*|\
      [Rr][Ee][Vv][Ii][Ee][Ww][Ee][Dd]-[Bb][Yy]:*|\
      [Aa][Cc][Kk][Ee][Dd]-[Bb][Yy]:*|\
      [Tt][Ee][Ss][Tt][Ee][Dd]-[Bb][Yy]:*|\
      [Ss][Uu][Gg][Gg][Ee][Ss][Tt][Ee][Dd]-[Bb][Yy]:*|\
      [Gg][Ee][Nn][Ee][Rr][Aa][Tt][Ee][Dd]-[Bb][Yy]:*|\
      [Aa][Ss][Ss][Ii][Ss][Tt][Ee][Dd]-[Bb][Yy]:*|\
      [Pp][Aa][Ii][Rr]-[Pp][Rr][Oo][Gg][Rr][Aa][Mm][Mm][Ee][Dd]-[Bb][Yy]:*)
        echo 'Blocked: commit messages may not include third-party attribution trailers.' >&2
        exit 1
        ;;
    esac
  done < "$message_file"
}

reject_staged_reserved_attribution() {
  reserved_part_one='co'
  reserved_part_two='dex'
  reserved_word="${reserved_part_one}${reserved_part_two}"

  if git diff --cached --no-ext-diff -U0 | while IFS= read -r diff_line; do
    case "$diff_line" in
      +[!+]*)
        case "$diff_line" in
          *"$reserved_word"*)
            echo 'Blocked: staged content includes a reserved third-party attribution reference.' >&2
            exit 1
            ;;
        esac
        ;;
    esac
  done
  then
    :
  else
    exit 1
  fi
}
