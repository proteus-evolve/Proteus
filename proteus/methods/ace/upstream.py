# Selected ACE functions, distributed under Apache-2.0.
# Copied from ace-agent/ace at 82709de050e1db6e6ef2f07bcb0393560b94992a.
# See LICENSE-ACE.txt and NOTICE.md. Executable bodies are unchanged; trailing
# whitespace is removed and module-local diagnostic print is suppressed.
# No SDK, embedding model or LLM loads.
"""Pinned ACE playbook functions. Do not fix upstream behavior in this profile."""

import re


def print(*args, **kwargs):
    """Suppress upstream debug output locally, without redirecting process stdout."""


def get_section_slug(section_name):
    """Convert section name to slug format (3-5 chars)"""
    # Common section mappings - updated to match original sections
    slug_map = {
        "financial_strategies_and_insights": "fin",
        "formulas_and_calculations": "calc",
        "code_snippets_and_templates": "code",
        "common_mistakes_to_avoid": "err",
        "problem_solving_heuristics": "prob",
        "context_clues_and_indicators": "ctx",
        "others": "misc",
        "meta_strategies": "meta"
    }

    # Clean and convert to snake_case
    clean_name = section_name.lower().strip().replace(" ", "_").replace("&", "and")

    if clean_name in slug_map:
        return slug_map[clean_name]

    # Generate slug from first letters
    words = clean_name.split("_")
    if len(words) == 1:
        return words[0][:4]
    else:
        return "".join(w[0] for w in words[:5])

def parse_playbook_line(line):
    """Parse a single playbook line to extract components"""
    # Pattern: [id] helpful=X harmful=Y :: content
    pattern = r'\[([^\]]+)\]\s*helpful=(\d+)\s*harmful=(\d+)\s*::\s*(.*)'
    match = re.match(pattern, line.strip())

    if match:
        return {
            'id': match.group(1),
            'helpful': int(match.group(2)),
            'harmful': int(match.group(3)),
            'content': match.group(4),
            'raw_line': line
        }
    return None

def get_next_global_id(playbook_text):
    """Extract highest global ID and return next one"""
    max_id = 0
    lines = playbook_text.strip().split('\n')

    for line in lines:
        parsed = parse_playbook_line(line)
        if parsed:
            # Extract numeric part from ID
            id_match = re.search(r'-(\d+)$', parsed['id'])
            if id_match:
                num = int(id_match.group(1))
                max_id = max(max_id, num)

    return max_id + 1

def format_playbook_line(bullet_id, helpful, harmful, content):
    """Format a bullet into playbook line format"""
    return f"[{bullet_id}] helpful={helpful} harmful={harmful} :: {content}"

def update_bullet_counts(playbook_text, bullet_tags):
    """Update helpful/harmful counts based on tags (Counter layer)"""
    lines = playbook_text.strip().split('\n')
    updated_lines = []

    # Create tag lookup - handle both old and new formats
    tag_map = {}
    if isinstance(bullet_tags, list) and len(bullet_tags) > 0:
        for tag in bullet_tags:
            if isinstance(tag, dict):
                # Handle both 'id' and 'bullet' keys for backwards compatibility
                bullet_id = tag.get('id') or tag.get('bullet', '')
                tag_value = tag.get('tag', 'neutral')
                if bullet_id:
                    tag_map[bullet_id] = tag_value

    if not tag_map:
        print("Warning: No valid bullet tags found to update counts")
        return playbook_text

    for line in lines:
        if line.strip().startswith('#') or not line.strip():
            # Preserve section headers and empty lines
            updated_lines.append(line)
            continue

        parsed = parse_playbook_line(line)
        if parsed and parsed['id'] in tag_map:
            tag = tag_map[parsed['id']]
            if tag == 'helpful':
                parsed['helpful'] += 1
            elif tag == 'harmful':
                parsed['harmful'] += 1
            # neutral: no change

            # Reconstruct line with updated counts
            new_line = format_playbook_line(
                parsed['id'], parsed['helpful'], parsed['harmful'], parsed['content']
            )
            updated_lines.append(new_line)
        else:
            updated_lines.append(line)

    return '\n'.join(updated_lines)

def apply_curator_operations(playbook_text, operations, next_id):
    """
    Apply curator operations to playbook

    TODO: Future Operations (not implemented yet)
    - UPDATE: Rewrite existing bullets to be more accurate or comprehensive
    - MERGE: Combine related bullets into stronger ones
    - CREATE_META: Add high-level strategy sections
    - DELETE: Remove outdated or incorrect bullets (if needed)
    """
    lines = playbook_text.strip().split('\n')

    # Build section map
    sections = {}
    current_section = "general"
    section_line_map = {}  # Track which line each section header is on

    for i, line in enumerate(lines):
        if line.strip().startswith('##'):
            # Extract section name and normalize it
            section_header = line.strip()[2:].strip()
            current_section = section_header.lower().replace(' ', '_').replace('&', 'and')
            section_line_map[current_section] = i
            if current_section not in sections:
                sections[current_section] = []
        elif line.strip():
            sections[current_section].append((i, line))

    # Process operations
    bullets_to_add = []

    for op in operations:
        op_type = op['type']

        # TODO: Future operation types (not implemented yet)
        # elif op_type == 'UPDATE':
        #     bullet_id = op.get('bullet_id', '')
                    #     new_content = op.get('content', '')
            #     bullets_to_update[bullet_id] = new_content
        # elif op_type == 'MERGE':
        #     source_ids = op.get('source_ids', [])
        #     bullets_to_delete.update(source_ids)
        #     # Add merged bullet logic here
        # elif op_type == 'CREATE_META':
        #     section_name = op.get('section_name', 'META_STRATEGIES')
        #     # Add meta section creation logic here

        if op_type == 'ADD':
            # Normalize section name from operation
            section_raw = op.get('section', 'general')
            section = section_raw.lower().replace(' ', '_').replace('&', 'and')

            # Check if section exists, if not use 'others'
            if section not in sections and section != 'general':
                print(f"Warning: Section '{section_raw}' not found, adding to OTHERS")
                section = 'others'

            slug = get_section_slug(section)
            new_id = f"{slug}-{next_id:05d}"
            next_id += 1

            content = op.get('content', '')

            new_line = format_playbook_line(new_id, 0, 0, content)
            bullets_to_add.append((section, new_line))
            print(f"  Added bullet {new_id} to section {section}")



    # Rebuild playbook
    new_lines = []
    for line in lines:
        parsed = parse_playbook_line(line)
        if parsed:
            new_lines.append(line)
        else:
            new_lines.append(line)

    # Add new bullets to appropriate sections
    final_lines = []
    current_section = None

    for line in new_lines:
        if line.strip().startswith('##'):
            # Before moving to new section, add any bullets for current section
            if current_section:
                section_adds = [b for s, b in bullets_to_add if s == current_section]
                final_lines.extend(section_adds)
                # Clear added bullets
                bullets_to_add = [(s, b) for s, b in bullets_to_add if s != current_section]

            section_header = line.strip()[2:].strip()
            current_section = section_header.lower().replace(' ', '_').replace('&', 'and')
        final_lines.append(line)

    # Add remaining bullets to current section
    if current_section:
        section_adds = [b for s, b in bullets_to_add if s == current_section]
        final_lines.extend(section_adds)
        bullets_to_add = [(s, b) for s, b in bullets_to_add if s != current_section]

    # If there are still bullets to add (for sections that don't exist), add them to OTHERS
    if bullets_to_add:
        print(f"Warning: {len(bullets_to_add)} bullets have no matching section, adding to OTHERS")
        others_bullets = [b for s, b in bullets_to_add]
        # Find OTHERS section
        others_idx = -1
        for i, line in enumerate(final_lines):
            if line.strip() == "## OTHERS":
                others_idx = i
                break

        if others_idx >= 0:
            # Insert after OTHERS header
            for i, bullet in enumerate(others_bullets):
                final_lines.insert(others_idx + 1 + i, bullet)
        else:
            # Append to end
            final_lines.extend(others_bullets)

    return '\n'.join(final_lines), next_id

def get_playbook_stats(playbook_text):
    """Generate statistics about the playbook"""
    lines = playbook_text.strip().split('\n')
    stats = {
        'total_bullets': 0,
        'high_performing': 0,  # helpful > 5, harmful < 2
        'problematic': 0,      # harmful >= helpful
        'unused': 0,           # helpful + harmful = 0
        'by_section': {}
    }

    current_section = 'general'

    for line in lines:
        if line.strip().startswith('##'):
            current_section = line.strip()[2:].strip()
            continue

        parsed = parse_playbook_line(line)
        if parsed:
            stats['total_bullets'] += 1

            if parsed['helpful'] > 5 and parsed['harmful'] < 2:
                stats['high_performing'] += 1
            elif parsed['harmful'] >= parsed['helpful'] and parsed['harmful'] > 0:
                stats['problematic'] += 1
            elif parsed['helpful'] + parsed['harmful'] == 0:
                stats['unused'] += 1

            if current_section not in stats['by_section']:
                stats['by_section'][current_section] = {'count': 0, 'helpful': 0, 'harmful': 0}

            stats['by_section'][current_section]['count'] += 1
            stats['by_section'][current_section]['helpful'] += parsed['helpful']
            stats['by_section'][current_section]['harmful'] += parsed['harmful']

    return stats

def extract_playbook_bullets(playbook_text, bullet_ids):
    """
    Extract specific bullet points from playbook based on bullet_ids.

    Args:
        playbook_text (str): The full playbook text
        bullet_ids (list): List of bullet IDs to extract

    Returns:
        str: Formatted playbook content containing only the specified bullets
    """
    if not bullet_ids:
        return "(No bullets used by generator)"

    lines = playbook_text.strip().split('\n')
    found_bullets = []

    for line in lines:
        if line.strip():  # Skip empty lines
            parsed = parse_playbook_line(line)
            if parsed and parsed['id'] in bullet_ids:
                found_bullets.append({
                    'id': parsed['id'],
                    'content': parsed['content'],
                    'helpful': parsed['helpful'],
                    'harmful': parsed['harmful']
                })

    if not found_bullets:
        return "(Generator referenced bullet IDs but none were found in playbook)"

    # Format the bullets for reflector input
    formatted_bullets = []
    for bullet in found_bullets:
        formatted_bullets.append(f"[{bullet['id']}] helpful={bullet['helpful']} harmful={bullet['harmful']} :: {bullet['content']}")

    return '\n'.join(formatted_bullets)
