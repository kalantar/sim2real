# Tab completion for sim2real deploy.py (bash)
#
# Usage — add to your .bashrc:
#   source /path/to/sim2real/pipeline/completions.bash
#
# Also registers completion for "python pipeline/deploy.py" via a wrapper.
# For aliases, register manually:  complete -F _sim2real_deploy my-alias

_sim2real_deploy() {
    local cur prev subcmd
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local subcommands="run status collect stop reset wipe pairs"
    local status_values="pending running done failed timed-out stalled"

    # Extract --experiment-root and --run values while finding the subcommand.
    subcmd=""
    local _exroot="" _run_name=""
    local i
    for ((i=1; i < COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            --experiment-root) ((i++)); _exroot="${COMP_WORDS[i]}" ;;
            --run)             ((i++)); _run_name="${COMP_WORDS[i]}" ;;
            -*) ;;
            run|status|collect|stop|reset|wipe|pairs)
                subcmd="${COMP_WORDS[i]}"
                break
                ;;
        esac
    done

    # Build as an array to avoid word-splitting on paths with spaces.
    local -a _deploy_cmd=("${PYTHON:-python}" pipeline/deploy.py)
    [[ -n "$_exroot" ]]    && _deploy_cmd+=(--experiment-root "$_exroot")
    [[ -n "$_run_name" ]]  && _deploy_cmd+=(--run "$_run_name")

    if [[ -z "$subcmd" ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "--run --experiment-root" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$subcommands" -- "$cur"))
        fi
        return
    fi

    case "$prev" in
        --only)
            local keys
            keys="$("${_deploy_cmd[@]}" pairs --keys-only 2>/dev/null)"
            COMPREPLY=($(compgen -W "$keys" -- "$cur"))
            return
            ;;
        --workload)
            local workloads
            workloads="$("${_deploy_cmd[@]}" pairs --workloads-only 2>/dev/null)"
            COMPREPLY=($(compgen -W "$workloads" -- "$cur"))
            return
            ;;
        --package)
            local packages
            packages="$("${_deploy_cmd[@]}" pairs --packages-only 2>/dev/null)"
            COMPREPLY=($(compgen -W "$packages" -- "$cur"))
            return
            ;;
        --status)
            COMPREPLY=($(compgen -W "$status_values" -- "$cur"))
            return
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        case "$subcmd" in
            run)
                COMPREPLY=($(compgen -W "--remote --only --workload --package --status --force --skip-build-epp --max-retries --poll-interval --gpu-resource-type --default-gpu-cost --pending-threshold --max-pending-stalls --max-backoff" -- "$cur"))
                ;;
            status)
                COMPREPLY=($(compgen -W "--only --workload --package --status" -- "$cur"))
                ;;
            reset)
                COMPREPLY=($(compgen -W "--only --workload --package --status --dry-run" -- "$cur"))
                ;;
            wipe)
                COMPREPLY=($(compgen -W "--only --workload --package --dry-run --yes" -- "$cur"))
                ;;
            collect)
                COMPREPLY=($(compgen -W "--only --workload --package --skip-logs" -- "$cur"))
                ;;
            pairs)
                COMPREPLY=($(compgen -W "--keys-only --workloads-only --packages-only" -- "$cur"))
                ;;
        esac
    fi
}

complete -F _sim2real_deploy deploy.py

# Handle "python pipeline/deploy.py ..." — bash sees "python" as the command,
# so we intercept and delegate when the first arg ends in deploy.py.
_python_deploy_py() {
    if [[ "${COMP_WORDS[1]}" == */deploy.py || "${COMP_WORDS[1]}" == deploy.py ]]; then
        local orig_words=("${COMP_WORDS[@]}")
        COMP_WORDS=("deploy.py" "${COMP_WORDS[@]:2}")
        (( COMP_CWORD -= 1 ))
        _sim2real_deploy
        COMP_WORDS=("${orig_words[@]}")
    fi
}

complete -F _python_deploy_py python
complete -F _python_deploy_py python3
