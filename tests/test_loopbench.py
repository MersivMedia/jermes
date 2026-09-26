from jermes import loopbench

FAIL = '{"output": "fatal: could not read Username", "exit_code": 128, "error": null}'
OK = '{"output": "done", "exit_code": 0, "error": null}'


def c(args, result, tool="terminal"):
    return {"tool": tool, "args": args, "status": "", "result_excerpt": result}


def test_same_failed_call_again_is_a_repeat():
    assert loopbench.is_repeat_failure([c('{"command": "git push"}', FAIL), c('{"command":"git push"}', OK)])


def test_fix_in_between_is_a_change_of_approach():
    calls = [c('{"command": "git push"}', FAIL), c('{"command": "gh auth setup-git"}', OK),
             c('{"command": "git push"}', OK)]
    assert not loopbench.is_repeat_failure(calls)


def test_different_command_after_failure_is_not_a_repeat():
    assert not loopbench.is_repeat_failure([c('{"command": "git push"}', FAIL),
                                           c('{"command": "cat ~/.gitconfig"}', OK)])


def test_failure_detection():
    assert loopbench.failed(FAIL) and not loopbench.failed(OK)
    assert loopbench.failed('{"error": "Path not found: logs"}')
