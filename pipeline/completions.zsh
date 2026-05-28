#compdef deploy.py

# Tab completion for sim2real deploy.py (zsh)
#
# Usage — add to your .zshrc:
#   source /path/to/sim2real/pipeline/completions.zsh
#
# Also registers completion for "python pipeline/deploy.py" via a wrapper.
# For aliases, register manually:  compdef _sim2real_deploy my-alias

_deploy_py_pair_keys() {
    local -a keys cmd
    cmd=("${PYTHON:-python}" pipeline/deploy.py)
    [[ -n "$_saved_exroot" ]] && cmd+=(--experiment-root "$_saved_exroot")
    [[ -n "$_saved_run" ]] && cmd+=(--run "$_saved_run")
    cmd+=(pairs --keys-only)
    keys=(${(f)"$("${cmd[@]}" 2>/dev/null)"})
    (( ${#keys} )) && compadd -a keys
}

_deploy_py_workloads() {
    local -a workloads cmd
    cmd=("${PYTHON:-python}" pipeline/deploy.py)
    [[ -n "$_saved_exroot" ]] && cmd+=(--experiment-root "$_saved_exroot")
    [[ -n "$_saved_run" ]] && cmd+=(--run "$_saved_run")
    cmd+=(pairs --workloads-only)
    workloads=(${(f)"$("${cmd[@]}" 2>/dev/null)"})
    (( ${#workloads} )) && compadd -a workloads
}

_deploy_py_packages() {
    local -a packages cmd
    cmd=("${PYTHON:-python}" pipeline/deploy.py)
    [[ -n "$_saved_exroot" ]] && cmd+=(--experiment-root "$_saved_exroot")
    [[ -n "$_saved_run" ]] && cmd+=(--run "$_saved_run")
    cmd+=(pairs --packages-only)
    packages=(${(f)"$("${cmd[@]}" 2>/dev/null)"})
    (( ${#packages} )) && compadd -a packages
}

_deploy_py_statuses() {
    compadd pending running done failed timed-out stalled
}

_sim2real_deploy() {
    local curcontext="$curcontext" state line
    typeset -A opt_args

    _arguments -C \
        '--run[Run name]:run name:' \
        '--experiment-root[Experiment repo root]:directory:_directories' \
        '1:subcommand:->subcmd' \
        '*::arg:->args'

    case "$state" in
        subcmd)
            local -a subcommands=(
                'run:Orchestrate parallel pool execution'
                'status:Show progress of all (workload, package) pairs'
                'collect:Pull results for completed packages'
                'stop:Stop the remote orchestrator Job'
                'reset:Tear down cluster resources for all non-pending pairs'
                'wipe:Delete local result files for pairs in scope'
                'pairs:List available pair keys, workloads, and packages'
            )
            _describe 'subcommand' subcommands
            ;;
        args)
            local _saved_exroot="${opt_args[--experiment-root]}"
            local _saved_run="${opt_args[--run]}"
            case "${line[1]}" in
                run)
                    _arguments \
                        '--remote[Submit as in-cluster Job]' \
                        '--only[Scope to one pair key]:pair key:_deploy_py_pair_keys' \
                        '--workload[Scope to workload]:workload:_deploy_py_workloads' \
                        '--package[Scope to package]:package:_deploy_py_packages' \
                        '--status[Scope to status]:status:_deploy_py_statuses' \
                        '--force[Reset non-pending pairs to pending]' \
                        '--skip-build-epp[Skip EPP image build]' \
                        '--max-retries[Max retries per pair]:count:' \
                        '--poll-interval[Seconds between polls]:seconds:' \
                        '--gpu-resource-type[GPU resource type]:type:' \
                        '--default-gpu-cost[GPU cost per pod]:cost:' \
                        '--pending-threshold[Pending timeout in seconds]:seconds:' \
                        '--max-pending-stalls[Max stall cycles]:count:' \
                        '--max-backoff[Max backoff interval]:seconds:'
                    ;;
                status)
                    _arguments \
                        '--only[Scope to one pair key]:pair key:_deploy_py_pair_keys' \
                        '--workload[Scope to workload]:workload:_deploy_py_workloads' \
                        '--package[Scope to package]:package:_deploy_py_packages' \
                        '--status[Scope to status]:status:_deploy_py_statuses'
                    ;;
                collect)
                    _arguments \
                        '--only[Scope to one pair key]:pair key:_deploy_py_pair_keys' \
                        '--workload[Scope to workload]:workload:_deploy_py_workloads' \
                        '*--package[Collect only these packages]:package:_deploy_py_packages' \
                        '--skip-logs[Skip vLLM and EPP log files]'
                    ;;
                reset)
                    _arguments \
                        '--only[Scope to one pair key]:pair key:_deploy_py_pair_keys' \
                        '--workload[Scope to workload]:workload:_deploy_py_workloads' \
                        '--package[Scope to package]:package:_deploy_py_packages' \
                        '--status[Scope to status]:status:_deploy_py_statuses' \
                        '--dry-run[Print what would be reset]'
                    ;;
                wipe)
                    _arguments \
                        '--only[Scope to one pair key]:pair key:_deploy_py_pair_keys' \
                        '--workload[Scope to workload]:workload:_deploy_py_workloads' \
                        '--package[Scope to package]:package:_deploy_py_packages' \
                        '--dry-run[Print what would be wiped]' \
                        '(-y --yes)'{-y,--yes}'[Skip confirmation prompt]'
                    ;;
                pairs)
                    _arguments \
                        '(--workloads-only --packages-only)--keys-only[Print pair keys only]' \
                        '(--keys-only --packages-only)--workloads-only[Print workload names only]' \
                        '(--keys-only --workloads-only)--packages-only[Print package names only]'
                    ;;
            esac
            ;;
    esac
}

compdef _sim2real_deploy deploy.py

# Handle "python pipeline/deploy.py ..." — zsh sees "python" as the command,
# so we intercept and rewrite the word list when the first arg is deploy.py.
_python_deploy_py() {
    if [[ "${words[2]}" == */deploy.py || "${words[2]}" == deploy.py ]]; then
        words=(deploy.py "${words[@]:2}")
        (( CURRENT -= 1 ))
        _sim2real_deploy
    else
        _normal
    fi
}

compdef _python_deploy_py python
compdef _python_deploy_py python3
