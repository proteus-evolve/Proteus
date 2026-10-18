"""Frozen text-only MathSolverSignature contract from DSPy 3.4.1.

The prompt/parser are restricted to the two string outputs of the reference
ChainOfThought program. This is not a general DSPy implementation.
Conformance tests compare the renderer/parser with the real pinned package.
The reflection template and math_metric come from GEPA commit
8b0ce6cd99a234f6b74daf37558a2ac0ce18f975.

DSPy source license:
MIT License

Copyright (c) 2023 Stanford Future Data Systems

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

GEPA source license:
Copyright © 2025 Lakshya A Agrawal

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."""

import inspect
import re
import textwrap

DSPY_VERSION = "3.4.1"
DSPY_WHEEL_SHA256 = "afda9e7f8b3d46be0829af19ac1153a6feaa6db5923f2b1011eef547fb15ce2d"

SYSTEM_PREFIX = 'Your input fields are:\n1. `input` (str): The math problem to solve.\nYour output fields are:\n1. `reasoning` (str): \n2. `answer` (str): The final numerical answer.\nAll interactions will be structured in the following way, with the appropriate values filled in.\n\n[[ ## input ## ]]\n{input}\n\n[[ ## reasoning ## ]]\n{reasoning}\n\n[[ ## answer ## ]]\n{answer}\n\n[[ ## completed ## ]]\nIn adhering to this structure, your objective is: '

USER_PREFIX = '[[ ## input ## ]]\n'

USER_SUFFIX = '\n\nRespond with the corresponding output fields, starting with the field `[[ ## reasoning ## ]]`, then `[[ ## answer ## ]]`, and then ending with the marker for `[[ ## completed ## ]]`.'

REFLECTION_TEMPLATE = "I am optimizing a parameter in my system. The current parameter value is:\n```\n<curr_param>\n```\n\nBelow is evaluation data showing how this parameter value performed across multiple test cases. The data contains performance metrics, diagnostic information, and other relevant details from the evaluation:\n```\n<side_info>\n```\n\nYour task is to propose a new, improved parameter value that can be used as a drop-in replacement for the current one.\n\nCarefully analyze all the evaluation data provided above. Look for patterns that indicate what works and what doesn't. Pay special attention to:\n- Performance metrics and how they correlate with parameter behavior\n- Recurring issues, errors, or failure patterns across multiple test cases\n- Successful patterns or behaviors that should be preserved or enhanced\n- Any domain-specific requirements, constraints, or factual information revealed in the evaluation data\n- Specific technical details that are crucial for understanding the parameter's role\n\nBased on your analysis, propose a new parameter value that addresses the identified issues while maintaining or improving upon what works well. Your proposal should be directly informed by the patterns and insights from the evaluation data.\n\nProvide the new parameter value within ``` blocks."


def messages(instructions, problem):
    # Signature.instructions is read through inspect.getdoc after assignment.
    objective = ("\n" + " " * 8).join([""] + textwrap.dedent(inspect.cleandoc(instructions)).splitlines())
    return [{"role": "system", "content": SYSTEM_PREFIX + objective},
            {"role": "user", "content": (USER_PREFIX + problem + USER_SUFFIX).strip()}]


def parse(completion):
    # Preserve the reference's first-field and inline-header behavior exactly.
    sections = [(None, [])]
    for line in completion.splitlines():
        match = re.match(r"\[\[ ## (\w+) ## \]\]", line.strip())
        if match:
            tail = line[match.end():].strip()
            sections.append((match.group(1), [tail] if tail else []))
        else:
            sections[-1][1].append(line)
    fields = {}
    for name, lines in sections:
        if name in ("reasoning", "answer") and name not in fields:
            fields[name] = "\n".join(lines).strip()
    if set(fields) != {"reasoning", "answer"}:
        raise ValueError("incomplete DSPy reasoning/answer fields")
    return fields


def math_metric(example, prediction):
    """Compute score and detailed feedback for math problems."""
    correct_answer, written_solution = int(example.answer), getattr(example, "solution", "")
    solution_suffix = (
        f" Here's the full step-by-step solution:\n{written_solution}\n\nThink about what takeaways you can learn from this solution to improve your future answers and approach to similar problems"
        if written_solution
        else ""
    )

    try:
        llm_answer = int(prediction.answer)
    except (ValueError, TypeError):
        feedback_text = f"The final answer must be a valid integer and nothing else. You responded with '{prediction.answer}', which couldn't be parsed as a python integer. Please ensure your answer is a valid integer without any additional text or formatting. The correct answer is '{correct_answer}'.{solution_suffix}{' and ensure your final answer is a valid integer.' if written_solution else ''}"
        return 0.0, feedback_text

    score = float(correct_answer == llm_answer)
    status = "correct" if score == 1.0 else "incorrect"
    feedback_text = f"Your answer is {status}. The correct answer is '{correct_answer}'.{solution_suffix}"
    return score, feedback_text
