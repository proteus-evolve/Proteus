# ruff: noqa: F841
# Selected source from ace-agent/ace@82709de050e1db6e6ef2f07bcb0393560b94992a.
# Apache-2.0; see proteus/methods/ace/LICENSE-ACE.txt and NOTICE.md.
# Executable bodies unchanged; trailing whitespace is removed.
"""Pinned Formula input parser and exact numeric/text correctness rule."""

def parse_context_and_question_formula(all_context):
    """Parse context to extract question and context parts for formula dataset

    Expected format:
    "[some instruction] Question: \"[QUESTION TEXT]\". Answer:"
    """
    if "Question: " in all_context and ". Answer:" in all_context:
        # Split by "Question: " to separate instruction from question
        parts = all_context.split("Question: ", 1)
        instruction_part = parts[0].strip()

        # Extract question text (between "Question: " and ". Answer:")
        question_part = parts[1]
        question_text = question_part.split(". Answer:")[0].strip()
        # Remove quotes if present
        if question_text.startswith('"') and question_text.endswith('"'):
            question_text = question_text[1:-1]
        question_text += " Your answer should be a plain floating point number, round to the nearest hundredth if necessary. Do the necessary conversions, for example 5 million should be 5000000.0. "
        return "", question_text

    return "", all_context


class FormulaReference:
    def _formula_answer_is_correct(self, predicted: str, ground_truth: str) -> bool:
        """formula dataset specific answer correctness check"""
        try:
            predicted = predicted.replace(",", "")
            ground_truth = ground_truth.replace(",", "")
            return float(predicted) == float(ground_truth)
        except Exception:
            return predicted == ground_truth
        return predicted == ground_truth
