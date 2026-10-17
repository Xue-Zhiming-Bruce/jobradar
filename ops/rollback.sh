#!/usr/bin/env bash
# Rollback: put the last commit Render reported as a successful deploy of this
# service back into production, and wait until it really is serving again.
#
# Usage, with the two values exported in the operator's own shell and never on
# the command line, where they would land in the shell's history:
#
#   ops/rollback.sh --dry-run   # print the two commits, send no deploy request
#   ops/rollback.sh             # deploy the target commit and wait for it
#
# The mechanism is deploy-by-commit, not Render's rollback endpoint
# (`POST /services/{id}/rollback`): Render only rolls back to a deploy whose
# retained build artifact still exists, and retention is a fixed number per plan
# (render.com/docs/rollbacks, "Build retention"), while deploying the commit
# builds it exactly as `deploy.yml` does. Do not "fix" this to the rollback
# endpoint.
#
# The target is never a git ref, a branch tip or a sha handed in as an argument:
# it is a `commit.id` read from Render's own deploy list, so an untested revision
# cannot reach production through this script. The list is read with `limit=20`
# and the script refuses rather than paging, so "not in the newest 20" is a
# refusal whose message says so and not a claim that the service has no history.
#
# The credential is used, never printed: no `set -x` and no `curl --verbose` on
# any step that holds it, and a failed call prints the HTTP status and the
# endpoint but never the response body, which could echo the key back. The
# repository is public (`_docs/decisions/2026-10-11-deploy-and-repo.md`).
#
# Plain `bash` for macOS's 3.2 as well as the runner's 5: no associative arrays,
# no `mapfile`, no `${var,,}`. `curl` and `jq` are the two tools, the same pair
# `deploy.yml` already uses.
set -euo pipefail

api="https://api.render.com/v1"

dry_run=0
for argument in "$@"; do
  case "${argument}" in
    --dry-run)
      dry_run=1
      ;;
    *)
      echo "error: unexpected argument; the only argument is --dry-run. The commit to deploy is read from Render's deploy list and is never passed in, so an argument is not read as one and is not echoed here." >&2
      exit 2
      ;;
  esac
done

missing=""
if [ -z "${RENDER_API_KEY:-}" ]; then
  missing="${missing} RENDER_API_KEY"
fi
if [ -z "${RENDER_SERVICE_ID:-}" ]; then
  missing="${missing} RENDER_SERVICE_ID"
fi
if [ -n "${missing}" ]; then
  echo "error: unset or empty:${missing}. Export both in your own shell and run this script again; nothing was sent to Render." >&2
  exit 1
fi

deploys_endpoint="${api}/services/${RENDER_SERVICE_ID}/deploys"
list_endpoint="${deploys_endpoint}?limit=20"

# Render's newest 20 deploys, newest first, each wrapped as
# `{"deploy": {"id", "status", "commit": {"id"}}}` -- or a non-zero exit naming
# the HTTP status (or curl's own exit code) and the endpoint it called.
deploy_list() {
  local response code curl_status
  response=$(curl --silent --show-error --max-time 60 --write-out '\n%{http_code}' \
    --header "Authorization: Bearer ${RENDER_API_KEY}" \
    "${list_endpoint}") || {
    curl_status=$?
    echo "error: GET ${list_endpoint} did not answer (curl exit ${curl_status}); nothing was sent to Render." >&2
    exit 1
  }
  code=${response##*$'\n'}
  if [ "${code}" != "200" ]; then
    echo "error: GET ${list_endpoint} answered HTTP ${code}; nothing was sent to Render. Check the key's scope and the service id." >&2
    exit 1
  fi
  if ! printf '%s' "${response%$'\n'*}" | jq -e 'type == "array"' >/dev/null 2>&1; then
    echo "error: GET ${list_endpoint} answered HTTP ${code} with a body that is not a JSON array; nothing was sent to Render." >&2
    exit 1
  fi
  printf '%s\n' "${response%$'\n'*}"
}

deploys=$(deploy_list)

# What is being served: the commit of the newest deploy Render reports as `live`.
serving=$(printf '%s' "${deploys}" | jq -r \
  '[.[] | .deploy | select(.status == "live")] | .[0].commit.id // empty')
if [ -z "${serving}" ]; then
  echo "error: ${list_endpoint} lists no deploy with status live, so nothing is being served and there is nothing to roll back from." >&2
  exit 1
fi

# The rollback target: the newest deploy that is a commit other than the one being
# served and that carries a status Render reports for a deploy whose build
# succeeded -- `live`, or `deactivated`, which is what a superseded deploy carries.
# Both spellings are accepted because which one the previous deploy carries
# depends only on whether Render flips the old deploy's status.
target=$(printf '%s' "${deploys}" | jq -r --arg serving "${serving}" \
  '[.[] | .deploy | select(.commit.id != $serving)
    | select(.status == "live" or .status == "deactivated") | .commit.id]
   | .[0] // empty')
if [ -z "${target}" ]; then
  echo "error: the newest 20 deploys (${list_endpoint}) hold no succeeded deploy of a commit other than the one being served (${serving}): there is nothing to roll back to in this page. Older deploys are not read -- raise the limit in this script and read the list again -- so this is not a claim that the service has no history." >&2
  exit 1
fi

echo "being served: ${serving}"
echo "rollback target: ${target}"

if [ "${dry_run}" -eq 1 ]; then
  echo "--dry-run: the selection above was made and no deploy request was sent; the service is untouched."
  exit 0
fi

# The deploy this script triggers is identified by a deploy id that was not in the
# list read above: the target commit is already listed there as its old deploy, so
# a match on `commit.id` alone would name the wrong one.
seen=$(printf '%s' "${deploys}" | jq -r '[.[] | .deploy | .id] | join("\n")')

# The call `deploy.yml` makes: it answers an empty body and no `Location` header,
# so whatever the status code, it names no deploy to poll.
post_code=$(curl --silent --show-error --max-time 60 --output /dev/null \
  --write-out '%{http_code}' \
  --request POST \
  --header "Authorization: Bearer ${RENDER_API_KEY}" \
  --header "Content-Type: application/json" \
  --data "{\"commitId\": \"${target}\"}" \
  "${deploys_endpoint}") || {
  curl_status=$?
  echo "error: POST ${deploys_endpoint} did not answer (curl exit ${curl_status}) for commit ${target}; whether the deploy was accepted is unknown, so check the Render dashboard." >&2
  exit 1
}
case "${post_code}" in
  2??)
    echo "Render accepted the deploy of ${target} (HTTP ${post_code}, empty body, no Location header)."
    ;;
  *)
    echo "error: POST ${deploys_endpoint} answered HTTP ${post_code} for commit ${target}, so no deploy was triggered. Check the key's scope and the service id." >&2
    exit 1
    ;;
esac

# The deploy is listed asynchronously, so absent is a retry until the wait runs
# out. 5 minutes to be listed and 15 to `live` are `deploy.yml`'s documented
# bounds: a free plan build takes minutes, and a deploy stuck longer than that is
# a fault rather than slowness.
deadline=$((SECONDS + 300))
deploy_id=""
while :; do
  deploys=$(deploy_list)
  deploy_id=$(printf '%s' "${deploys}" | jq -r --arg sha "${target}" --arg seen "${seen}" \
    '[.[] | .deploy | . as $deploy
      | select($deploy.commit.id == $sha)
      | select(($seen | split("\n") | index($deploy.id)) == null)
      | $deploy.id] | .[0] // empty')
  if [ -n "${deploy_id}" ]; then
    echo "the deploy triggered here is ${deploy_id} (${target}), listed after ${SECONDS}s."
    break
  fi
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "error: Render accepted the deploy of ${target} (HTTP ${post_code}) but lists no new deploy of that commit within 5 minutes, so nothing was rolled back. Check the Render dashboard." >&2
    exit 1
  fi
  echo "no new deploy of ${target} listed after ${SECONDS}s; retrying."
  sleep 10
done

deadline=$((SECONDS + 900))
while :; do
  deploys=$(deploy_list)
  status=$(printf '%s' "${deploys}" | jq -r --arg id "${deploy_id}" \
    '[.[] | .deploy | select(.id == $id)] | .[0].status // empty')
  case "${status}" in
    live)
      echo "deploy ${deploy_id} (${target}) is live after ${SECONDS}s: production is back on ${target}."
      exit 0
      ;;
    canceled | deactivated | build_failed | update_failed | pre_deploy_failed)
      echo "error: deploy ${deploy_id} (${target}) ended ${status}, so production is not on the target commit. Check the Render build log." >&2
      exit 1
      ;;
  esac
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "error: deploy ${deploy_id} (${target}) was still ${status:-missing from the list} after 15 minutes. A free plan build takes minutes, not a quarter of an hour, so this is a fault; check the Render build log." >&2
    exit 1
  fi
  echo "deploy ${deploy_id} is ${status:-missing from the list} after ${SECONDS}s; retrying."
  sleep 15
done
