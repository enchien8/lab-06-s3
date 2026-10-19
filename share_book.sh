#!/bin/bash
set -euo pipefail

TEMP_DOWNLOAD_PATH=""

# Keep progress on stderr so stdout contains only the presigned URL.
log() {
    local level="$1"
    shift
    printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "$*" >&2
}

# Remove only the incomplete temporary download created by this run.
cleanup() {
    if [[ -n "$TEMP_DOWNLOAD_PATH" ]]; then
        rm -f -- "$TEMP_DOWNLOAD_PATH" || true
    fi
}
trap cleanup EXIT

# Download one Gutenberg book, upload it to a private bucket, and presign it.
main() {
    if [[ "$#" -ne 3 ]]; then
        log ERROR "Usage: $0 GUTENBERG_TEXT_URL PRIVATE_BUCKET EXPIRATION_SECONDS"
        exit 1
    fi

    local book_url="$1"
    local bucket="$2"
    local expiration_input="$3"

    if [[ "$book_url" != https://www.gutenberg.org/* || "$book_url" != *.txt ]]; then
        log ERROR "Book URL must be a Project Gutenberg plain-text .txt URL."
        exit 1
    fi

    if [[ ! "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ || "$bucket" == *..* || "$bucket" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        log ERROR "Bucket must be a valid 3-63 character S3 bucket name."
        exit 1
    fi

    if [[ ! "$expiration_input" =~ ^[0-9]+$ ]]; then
        log ERROR "Expiration must be a positive whole number of seconds (maximum 604800)."
        exit 1
    fi
    local expiration="$expiration_input"
    while [[ "$expiration" == 0* && "${#expiration}" -gt 1 ]]; do
        expiration="${expiration#0}"
    done
    if [[ "${#expiration}" -gt 6 ]] || (( expiration < 1 || expiration > 604800 )); then
        log ERROR "Expiration must be between 1 and 604800 seconds."
        exit 1
    fi

    local book_name="${book_url##*/}"
    if [[ -z "$bucket" || -z "$book_name" ]]; then
        log ERROR "Bucket name and book URL must not be empty."
        exit 1
    fi

    for required_command in curl aws; do
        if ! command -v "$required_command" >/dev/null 2>&1; then
            log ERROR "Required command not found: $required_command"
            exit 1
        fi
    done

    # Download atomically so a failed request cannot leave a partial cached book.
    mkdir -p -- books
    local book_path="books/$book_name"
    TEMP_DOWNLOAD_PATH="${book_path}.part.$$"
    if ! curl --fail --location --silent --show-error \
        --output "$TEMP_DOWNLOAD_PATH" "$book_url"; then
        log ERROR "Could not download $book_url"
        exit 1
    fi
    if [[ ! -s "$TEMP_DOWNLOAD_PATH" ]]; then
        log ERROR "The downloaded book is empty: $book_url"
        exit 1
    fi
    if ! mv -- "$TEMP_DOWNLOAD_PATH" "$book_path"; then
        log ERROR "Could not save the book to $book_path"
        exit 1
    fi
    TEMP_DOWNLOAD_PATH=""
    log INFO "Downloaded $book_path"

    # Reuse an accessible bucket; otherwise create it in the course region.
    if aws s3 ls "s3://$bucket" >/dev/null 2>&1; then
        log INFO "Using existing private bucket $bucket"
    elif ! aws s3 mb "s3://$bucket" --region us-east-1 >&2; then
        log ERROR "Could not create bucket $bucket"
        exit 1
    else
        log INFO "Created private bucket $bucket"
    fi

    # Upload the object without changing the bucket's private access settings.
    if ! aws s3 cp "$book_path" "s3://$bucket/$book_name" \
        --content-type "text/plain; charset=utf-8" >&2; then
        log ERROR "Could not upload $book_path to s3://$bucket/$book_name"
        exit 1
    fi
    log INFO "Uploaded s3://$bucket/$book_name as a private object"

    # Print only the signed URL to stdout; progress messages stay on stderr.
    local presigned_url
    if ! presigned_url="$(aws s3 presign "s3://$bucket/$book_name" --expires-in "$expiration")"; then
        log ERROR "Could not create a presigned URL for s3://$bucket/$book_name"
        exit 1
    fi
    if [[ -z "$presigned_url" ]]; then
        log ERROR "AWS CLI returned an empty presigned URL."
        exit 1
    fi

    log INFO "Presigned URL created; requested expiration is $expiration seconds."
    printf '%s\n' "$presigned_url"
}

main "$@"
