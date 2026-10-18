# ruff: noqa: F541
# Selected ACE parsers from ace-agent/ace at 82709de050e1db6e6ef2f07bcb0393560b94992a.
# Apache-2.0; see LICENSE-ACE.txt and NOTICE.md. Executable bodies unchanged;
# only trailing docstring whitespace and module-local diagnostics are normalized.
"""Pinned ACE response parsers, without upstream model clients."""

import json
import re
from typing import Any, Dict, List

from proteus.methods.ace.upstream import print


def extract_boxed_content(text):
    """Helper function to extract content from \\boxed{} format"""
    pattern = r'\\boxed\{'
    match = re.search(pattern, text)
    if not match:
        return None

    start = match.end() - 1  # Position of opening brace
    brace_count = 0
    i = start

    while i < len(text):
        if text[i] == '{':
            brace_count += 1
        elif text[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                return text[start + 1:i]  # Content between braces
        i += 1
    return None

def extract_answer(response):
    """Extract final answer from model response"""
    try:
        # First try JSON parsing
        parsed = json.loads(response)
        answer = str(parsed.get("final_answer", "No final answer found"))
        return answer

    except (json.JSONDecodeError, KeyError, AttributeError):
        # JSON parsing failed, use fallback logic
        matches = re.findall(r"Finish\[(.*?)\]", response)
        if matches:
            answer = matches[-1]
            return answer

        # Try to get final answer from JSON style response with regex matching
        # Try double quotes first
        matches = re.findall(r'"final_answer"\s*:\s*"([^"]*)"', response)
        if matches:
            answer = matches[-1]
            return answer

        # Try single quotes
        matches = re.findall(r"'final_answer'\s*:\s*'([^']*)'", response)
        if matches:
            answer = matches[-1]
            return answer

        # Handle JSON format without quotes (for simple expressions)
        matches = re.findall(r'[\'"]final_answer[\'"]\s*:\s*([^,}]+)', response)
        if matches:
            answer = matches[-1].strip()
            # Clean up trailing characters
            answer = re.sub(r'[,}]*$', '', answer)
            return answer

        # Fallback for "The final answer is: X" pattern with boxed
        final_answer_pattern = r'[Tt]he final answer is:?\s*\$?\\boxed\{'
        match = re.search(final_answer_pattern, response)
        if match:
            # Extract boxed content starting from this match
            remaining_text = response[match.start():]
            boxed_content = extract_boxed_content(remaining_text)
            if boxed_content:
                return boxed_content

        # More general pattern for "final answer is X"
        matches = re.findall(r'[Tt]he final answer is:?\s*([^\n.]+)', response)
        if matches:
            answer = matches[-1].strip()
            # Clean up common formatting
            answer = re.sub(r'^\$?\\boxed\{([^}]+)\}\$?$', r'\1', answer)
            answer = answer.replace('$', '').strip()
            if answer:
                return answer

        return "No final answer found"

def extract_json_from_text(text, json_key=None):
    """Extract JSON object from text, handling various formats"""
    try:
        # First, try to parse the entire response as JSON (JSON mode)
        try:
            result = json.loads(text.strip())
            return result
        except json.JSONDecodeError:
            pass

        # Fallback: Look for ```json blocks
        json_pattern = r'```json\s*(.*?)\s*```'
        matches = re.findall(json_pattern, text, re.DOTALL | re.IGNORECASE)

        if matches:
            # Try each match until we find valid JSON
            for match in matches:
                try:
                    json_str = match.strip()
                    result = json.loads(json_str)
                    return result
                except json.JSONDecodeError:
                    continue

        # Improved JSON extraction using balanced brace counting
        # This handles deeply nested structures better
        def find_json_objects(text):
            """Find JSON objects using balanced brace counting"""
            json_objects = []
            i = 0
            while i < len(text):
                if text[i] == '{':
                    # Found start of potential JSON object
                    brace_count = 1
                    start = i
                    i += 1

                    while i < len(text) and brace_count > 0:
                        if text[i] == '{':
                            brace_count += 1
                        elif text[i] == '}':
                            brace_count -= 1
                        elif text[i] == '"':
                            # Handle quoted strings to avoid counting braces inside strings
                            i += 1
                            while i < len(text) and text[i] != '"':
                                if text[i] == '\\':
                                    i += 1  # Skip escaped character
                                i += 1
                        i += 1

                    if brace_count == 0:
                        # Found complete JSON object
                        json_candidate = text[start:i]
                        json_objects.append(json_candidate)
                else:
                    i += 1

            return json_objects

        # Find all potential JSON objects
        json_objects = find_json_objects(text)

        for json_str in json_objects:
            try:
                result = json.loads(json_str)
                return result
            except json.JSONDecodeError:
                continue

    except Exception as e:
        print(f"Failed to extract JSON: {e}")
        if len(text) > 500:
            print(f"Raw content preview:\n{text[:500]}...")
        else:
            print(f"Raw content:\n{text}")

    return None

class Generator:
    def _extract_bullet_ids(self, response: str, use_json_mode: bool) -> List[str]:
        """
        Extract bullet IDs from generator response.

        Args:
            response: The generator's response
            use_json_mode: Whether JSON mode was used

        Returns:
            List of bullet IDs
        """
        bullet_ids = []

        if use_json_mode:
            try:
                response_json = json.loads(response)
                bullet_ids = response_json.get("bullet_ids", [])
            except (json.JSONDecodeError, KeyError):
                # If parsing fails, try regex extraction
                bullet_ids = self._extract_bullet_ids_regex(response)
        else:
            bullet_ids = self._extract_bullet_ids_regex(response)

        return bullet_ids

    def _extract_bullet_ids_regex(self, text: str) -> List[str]:
        """
        Extract bullet IDs using regex pattern matching.

        Args:
            text: Text to extract bullet IDs from

        Returns:
            List of bullet IDs
        """
        # Pattern matches: [xxx-00001], [abc-00042], etc.
        pattern = r'\[([a-z]{3,}-\d{5})\]'
        matches = re.findall(pattern, text)
        return matches

class Reflector:
    def _extract_bullet_tags(
        self,
        response: str,
        use_json_mode: bool
    ) -> List[Dict[str, str]]:
        """
        Extract bullet tags from reflector response.

        Args:
            response: The reflector's response
            use_json_mode: Whether JSON mode was used

        Returns:
            List of dicts with 'id' and 'tag' keys
        """
        bullet_tags = []

        if use_json_mode:
            try:
                response_json = json.loads(response)
                bullet_tags = response_json.get("bullet_tags", [])
            except (json.JSONDecodeError, KeyError):
                print(f"Warning: Failed to parse bullet tags from JSON response")
        else:
            # Try to extract from non-JSON response
            # This is a fallback and may not always work
            try:
                # Look for JSON-like structure in the response
                start_idx = response.find('"bullet_tags"')
                if start_idx != -1:
                    # Find the array
                    bracket_idx = response.find('[', start_idx)
                    if bracket_idx != -1:
                        # Find matching closing bracket
                        depth = 0
                        end_idx = bracket_idx
                        for i in range(bracket_idx, len(response)):
                            if response[i] == '[':
                                depth += 1
                            elif response[i] == ']':
                                depth -= 1
                                if depth == 0:
                                    end_idx = i + 1
                                    break

                        bullet_tags_str = response[bracket_idx:end_idx]
                        bullet_tags = json.loads(bullet_tags_str)
            except Exception as e:
                print(f"Warning: Failed to extract bullet tags: {e}")

        return bullet_tags

class Curator:
    def _extract_and_validate_operations(
        self,
        response: str
    ) -> Dict[str, Any]:
        """
        Extract and validate operations from curator response.

        Args:
            response: The curator's response

        Returns:
            Dictionary with 'reasoning' and 'operations' keys

        Raises:
            ValueError: If JSON is invalid or missing required fields
        """
        # Extract operations info
        operations_info = extract_json_from_text(response, "operations")

        # Validate JSON structure is correct
        if not operations_info:
            raise ValueError("Failed to extract valid JSON from curator response")

        # Validate required fields
        if "reasoning" not in operations_info:
            raise ValueError("JSON missing required 'reasoning' field")

        if "operations" not in operations_info:
            raise ValueError("JSON missing required 'operations' field")

        # Validate field types
        if not isinstance(operations_info["reasoning"], str):
            raise ValueError("'reasoning' field must be a string")

        if not isinstance(operations_info["operations"], list):
            raise ValueError("'operations' field must be a list")

        # Validate operations structure
        for i, op in enumerate(operations_info["operations"]):
            if not isinstance(op, dict):
                raise ValueError(f"Operation {i} must be a dictionary")

            if "type" not in op:
                raise ValueError(f"Operation {i} missing required 'type' field")

            op_type = op["type"]

            # Currently only ADD operations are fully supported
            # Note: You can add support for UPDATE, MERGE, DELETE operations here
            if op_type not in ["ADD", "UPDATE", "MERGE", "DELETE", "CREATE_META"]:
                print(f"Warning: Operation type '{op_type}' may not be fully supported")

            # Validate ADD operation structure
            if op_type == "ADD":
                required_fields = {"type", "section", "content"}
                missing_fields = required_fields - set(op.keys())
                if missing_fields:
                    raise ValueError(f"ADD operation {i} missing fields: {list(missing_fields)}")

        return operations_info
