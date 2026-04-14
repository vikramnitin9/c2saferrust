import argparse
import json
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import matplotlib.pyplot as plt
import networkx as nx

from models import ModelException, get_model_from_name


def prRed(skk): print("\033[91m {}\033[00m" .format(skk))
def prGreen(skk): print("\033[92m {}\033[00m" .format(skk))
def prCyan(skk): print("\033[96m {}\033[00m" .format(skk))
def prYellow(skk): print("\033[93m {}\033[00m" .format(skk))
def prLightPurple(skk): print("\033[94m {}\033[00m" .format(skk))
def prLightGray(skk): print("\033[97m {}\033[00m" .format(skk))

def run_slicer(code_dir, ofile_name, max_lines=150):
    # Run slicer to create program slices
    prCyan("Running slicer")
    cwd = os.getcwd()
    cmd = 'cd {} && RUSTFLAGS="-Awarnings" cargo slicer -- -Zoutput-file={} -Zmax-lines={}'.format(code_dir, ofile_name, max_lines)
    os.system(cmd)
    os.chdir(cwd)

def run_dataflow(code_dir, ofile_name):
    # Run dataflow to create program slices
    prCyan("Running dataflow analysis")
    cwd = os.getcwd()
    cmd = 'cd {} && RUSTFLAGS="-Awarnings" cargo slicer -- -Zdataflow-only -Zinput-file={} -Zoutput-file={}'.format(code_dir, ofile_name, ofile_name)
    status = os.system(cmd)
    if os.WEXITSTATUS(status) != 0:
        prRed("Dataflow analysis failed")
        import pdb; pdb.set_trace()
    os.chdir(cwd)

def extract_c_body(func, c_code_dir):
    fpath = Path(os.path.join(c_code_dir, func['filename']))
    start_line = func['startLine']
    start_col = func['startCol']
    end_line = func['endLine']
    end_col = func['endCol']

    with open(fpath, 'r') as f:
        lines = f.readlines()
    
    body = lines[start_line-1][start_col-1:]
    for i in range(start_line, end_line-1):
        body += lines[i]
    body += lines[end_line-1][:end_col]
    return body


def parse_span(span):
    # The span string looks like this: "src/xmalloc.rs:116:1: 125:2 (#0)"
    # We need to extract the file name, the line numbers, and the column numbers
    # Note that both are 1-indexed
    span_parts = span.split(':')
    file_name = span_parts[0]
    start_line = int(span_parts[1])
    start_col = int(span_parts[2])
    end_line = int(span_parts[3].strip())
    end_col = int(span_parts[4].split()[0])

    if len(span.split(':')[4].split()) > 1:
        appendage = span.split(':')[4].split()[1]
    else:
        appendage = ''

    return {'file_name': file_name,
            'start_line': start_line,
            'start_col': start_col,
            'end_line': end_line,
            'end_col': end_col,
            'appendage': appendage}

def span_to_text(code_dir, span, old=False):
    span = parse_span(span)
    original_fpath = Path(code_dir, span['file_name'])
    if old:
        fpath = original_fpath.with_suffix('.old')
    else:
        fpath = original_fpath
    with open(fpath, 'r') as f:
        lines = f.readlines()
    if span['start_line'] == span['end_line']:
        return lines[span['start_line'] - 1][span['start_col'] - 1 : span['end_col']]
    else:
        return lines[span['start_line'] - 1][span['start_col'] - 1 :] + \
                ''.join(lines[span['start_line'] : span['end_line'] - 1]) + \
                lines[span['end_line'] - 1][: span['end_col']]


def replace(code_dir, spans, replacements, imports):
    # We have to do all the replacements at once because the spans will change after each replacement

    prCyan("Replacing:")
    print('\n'.join(spans))

    per_file_replacements = {}

    for span, replacement in zip(spans, replacements):
        span = parse_span(span)
        file_name, (start_line, start_col, end_line, end_col) = \
            span['file_name'], (span['start_line'], span['start_col'], span['end_line'], span['end_col'])
        per_file_replacements[file_name] = per_file_replacements.get(file_name, []) + \
                            [((start_line, start_col, end_line, end_col), replacement)]
        
    for file_name, file_spans in per_file_replacements.items():
        # Read the file
        original_fpath = Path(code_dir, file_name)
        if original_fpath.with_suffix('.old').exists():
            fpath = original_fpath.with_suffix('.old')
        else:
            fpath = original_fpath
        with open(fpath, 'r') as f:
            lines = f.readlines()

        # Sort the spans by the starting line number
        file_spans.sort(key=lambda x: (x[0][0], x[0][1]))
        
        # Make a list of pieces
        pieces = []
        previous_line, previous_col = 1, 1 # All 1-indexed
        for i, ((start_line, start_col, end_line, end_col), replacement) in enumerate(file_spans):
            if previous_line > len(lines):
                prCyan("Something wrong")
                import pdb; pdb.set_trace()
            pieces +=   [lines[previous_line-1][previous_col-1:]] + \
                        lines[previous_line:(start_line-1)] + \
                        [lines[start_line-1][:start_col-1]] + \
                        [replacement]
            previous_line, previous_col = end_line, end_col
        
        if previous_line > len(lines):
            prCyan("Something wrong")
            import pdb; pdb.set_trace()
        pieces += [lines[previous_line-1][previous_col-1:]] + lines[previous_line:]

        new_contents = ''.join(pieces)
        
        # Here, we are relying on the fact that the function translation is span[0]
        # So the imports need to be inserted into the file corresponding to that span.
        if spans[0].split(':')[0] == file_name:
            new_lines = new_contents.split('\n')
            inside_attribute = False
            for i, line in enumerate(new_lines):
                # Check if line starts with "#!" (inner attribute)
                # These could also extend over multiple lines, which is tricky
                # Like #![feature(try_blocks), 
                #       feature(never_type)]
                if inside_attribute:
                    if ']' in line:
                        inside_attribute = False
                    continue
                if line.startswith('#!'):
                    if ']' not in line:
                        inside_attribute = True
                    continue
                # There can be empty lines between inner attribute lines
                if line.strip() == '':
                    continue
                break
            new_lines.insert(i, imports)
            new_contents = '\n'.join(new_lines)

        # Write the file back
        with open(original_fpath, 'w') as f:
            f.write(new_contents)
        
        with open(fpath.with_suffix('.old'), 'w') as f:
            f.write(''.join(lines))

class CompileException(Exception):
    pass

class RunException(Exception):
    pass

def compile(code_dir, verbose=False):
    cwd = os.getcwd()

    cargo_commands = [
        'cd {} && RUSTFLAGS="-Awarnings" cargo check'.format(code_dir),
        'cd {} && RUSTFLAGS="-Awarnings" cargo build --offline'.format(code_dir)
    ]

    for cmd in cargo_commands:
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                timeout=20,
                stderr=subprocess.STDOUT if verbose else subprocess.PIPE,
                stdout=None if verbose else subprocess.PIPE,
            )
            if result.returncode != 0:
                compiler_output = result.stderr.decode("utf-8", errors="ignore")
                raise CompileException(compiler_output)
        except (subprocess.TimeoutExpired, TimeoutError):
            raise CompileException("{} timed out".format(cmd))
        finally:
            os.chdir(cwd)

def cleanup(code_dir):
    cwd = os.getcwd()
    os.chdir(code_dir)
    cmd = 'rm -rf target'
    try:
        run(cmd)
    except RuntimeError as e:
        # Read e and look for strings that say:
        # rm: cannot remove '<filename>': Device or resource busy
        # Get a list of these filenames
        for line in str(e).split('\n'):
            if 'cannot remove' in line and "Device or resource busy" in line:
                filename = line.split('\'')[1]
                try:
                    run(f'fuser -k {filename}')
                except:
                    pass
                try:
                    run(f'rm -rf {filename}')
                except:
                    pass
        try:
            run(cmd)
        except RuntimeError as e:
            prRed(f"Failed to fully cleanup {code_dir}")
    os.chdir(cwd)

def select_tests(code_dir, test_dir):
    test_list = []
    if test_dir == "":
        return test_list
    
    binary_path = os.path.join(code_dir, f'target/debug')

    for test_name in os.listdir(test_dir):
        if not test_name.endswith('.sh'):
            continue
        cmd = f'{os.path.join(test_dir, test_name)} {binary_path}'
        try:
            run(cmd)
            prGreen(f"Test {test_name} passed")
            test_list.append(test_name)
        except RuntimeError as e:
            prRed(f"Test {test_name} failed")    
    return test_list

def run(command):

    try:
        result = subprocess.run(
            command,
            shell=True,
            timeout=25,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        if result.returncode != 0:
            exec_output = result.stderr.decode('utf-8', errors='ignore')
            if exec_output.strip() == '':
                exec_output = result.stdout.decode('utf-8', errors='ignore')
            raise RuntimeError(exec_output)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout")

def construct_prompt_for_func(func):

    if len(func['calls']) > 0:
        call_sites = "Here are its call sites\n" + '\n'.join([
f'''Call site {i+1}:
```rust
{call['source']}
```''' for i, call in enumerate(func['calls']) if call['caller'] != func['func_defid']])
        call_site_instruction = ("If the function signature changed in translation, its callsites will need to be modified as well. "
                                 "Place each callsite translation (in the same order it appears above) between <CALL> and </CALL>. "
                                 "Note that even if the callsite is only a single statement, the translation can be mutiple statements. "
                                 "For example, you may need to declare new variables, or convert between types, either before or after the call. "
                                 "The translation should be such that the surrounding code is not affected by the changes.")
    else:
        call_sites = ""
        call_site_instruction = ""
    
    if len(func['imports']) > 0:
        imports = "The file contains the following imports:\n" + '\n'.join([
f'''```rust
{import_['source']}
```''' for import_ in func['imports']])
    else:
        imports = ""
    
    if len(func['globals']) > 0:
        globals = "The function uses the following global variables:\n" + '\n'.join([
f'''```rust
{global_['source']}
```''' for global_ in func['globals']])
    else:
        globals = ""

    if len(func['sub_chunks']) > 0:
        chunk_instruction = ("There are some pieces of code that are not shown here, in comments between the tags <CHUNK> and </CHUNK>. "
                             "Note the variables that are live at the beginning and end of each chunk, and ensure that the translation of the surrounding code maintains these variables. "
                             "You cannot change the variables that are live at the beginning and end of each chunk."
                             "In your translation, make sure that these comments containing chunks are preserved. "
                             "In other words, keep the portions with /* <CHUNK> ... </CHUNK> */ unchanged.")
    else:
        chunk_instruction = ""

    prompt = f'''Here is a function:
```rust
{func['source']}
```
{call_sites}
{globals}
{imports}
Convert the function to idiomatic Rust, meaning Rust code that does not make use of features like unsafe, raw pointers, and the C API whenever possible. Do not change the function name.

Follow the following format for your output: Place the function translation between the tags <FUNC> and </FUNC>. {call_site_instruction}

{chunk_instruction}
Any functions or variables without definitions are defined elsewhere in the code. Do not attempt to redefine them or import them.
If you are using any new functions and you need new imports for those, place them between the tags <IMPORTS> and </IMPORTS>. This should be only *new* imports. Do not include existing imports.
DO NOT include markdown characters like "```" or "```rust" in your translation.
'''

    return prompt

def construct_prompt_for_chunk(chunk, globals, imports):

    if len(chunk['sub_chunks']) > 0:
        chunk_instruction = ("There are some pieces of code that are not shown here, between the tags <CHUNK> and </CHUNK>. "
                             "Note the variables that are live at the beginning and end of each chunk, and ensure that the translation of the surrounding code maintains these variables. "
                             "You cannot change the variables that are live at the beginning and end of each chunk. "
                             "In your translation, make sure that these comments containing chunks are preserved. "
                             "In other words, keep the portions with /* <CHUNK> ... </CHUNK> */ unchanged.")
    else:
        chunk_instruction = ""

    if len(imports) > 0:
        imports = "The file contains the following imports:\n" + '\n'.join([
f'''```rust
{import_['source']}
```''' for import_ in imports])
    else:
        imports = ""
    
    if len(globals) > 0:
        globals = "The function uses the following global variables:\n" + '\n'.join([
f'''```rust
{global_['source']}
```''' for global_ in globals])
    else:
        globals = ""
    
    prompt = f'''Here is a piece of code from a larger function:
```rust
{chunk['source']}
```
{globals}
{imports}
Convert this piece of code to idiomatic Rust, meaning Rust code that does not make use of features like unsafe, raw pointers, and the C API whenever possible.
Follow the following format for your output: Place the translation between the tags <FUNC> and </FUNC>.
{chunk_instruction}
Any functions or variables without definitions are defined elsewhere in the code. Do not attempt to redefine them or import them.
If you are using any new functions and you need new imports for those, place them between the tags <IMPORTS> and </IMPORTS>. This should be only *new* imports. Do not include existing imports.
DO NOT include markdown characters like "```" or "```rust" in your translation.
'''

    return prompt

def annotate_with_liveness_info(chunk, chunk_list):

    for sub_chunk_num in chunk['sub_chunks']:
        replacement_text = f'''
        /* <CHUNK>
        Some code here that uses the following variables:
        ({', '.join(chunk_list[sub_chunk_num]['live_in'])})
        At the end of this chunk, the following variables are live:
        ({', '.join(chunk_list[sub_chunk_num]['live_out'])})
        </CHUNK> */
        '''.strip()
        chunk['source'] = chunk['source'].replace(f"<<chunk {sub_chunk_num}>>", replacement_text)

    return chunk

def preprocess_func(func):

    func = annotate_with_liveness_info(func, func['chunks'])
        
    for chunk in func['chunks']:
        chunk = annotate_with_liveness_info(chunk, func['chunks'])
        chunk['source'] = f'''
/*
The variables live at this point are:
({', '.join(chunk['live_in'])})
*/
{chunk['source']}
/*
The variables live at this point are:
({', '.join(chunk['live_out'])})
*/
'''.strip()
    # Remove calls that are recursive calls
    func['calls'] = [call for call in func['calls'] if call['caller'] != func['func_defid']]

    return func

def modify_span(span_text, old_spans, replacements, new_imports):

    assert len(old_spans) == len(replacements)
    span_to_modify = parse_span(span_text)

    # Here, we are relying on the fact that the function translation is old_spans[0]
    if old_spans[0].split(':')[0] == span_to_modify['file_name']:
        start_line_offset = new_imports.count('\n') + 1
    else:
        start_line_offset = 0

    end_line_offset = 0
    start_col_offset = 0
    end_col_offset = 0

    old_spans = [parse_span(s) for s in old_spans]
    # We want only the replacements that are in the same file
    selected = [(s, r) for s, r in zip(old_spans, replacements) if s['file_name'] == span_to_modify['file_name']]
    if len(selected) == 0:
        return span_text
    old_spans, replacements = zip(*selected)
    # Sort the spans by the starting line number
    old_spans, replacements = zip(*sorted(zip(old_spans, replacements), key=lambda x: (x[0]['start_line'], x[0]['start_col'])))
    old_spans, replacements = list(old_spans), list(replacements)

    internal = False

    for old_span, replacement in zip(old_spans, replacements):
        # Check if this old_span ends before the start of the span_to_modify
        if (old_span['end_line'] < span_to_modify['start_line']) or \
                (old_span['end_line'] == span_to_modify['start_line'] and old_span['end_col'] < span_to_modify['start_col']):
            
            start_line_offset += replacement.count('\n') - (old_span['end_line'] - old_span['start_line'])
            # Check if this old span ends exactly on the start line of the span_to_modify
            if old_span['end_line'] == span_to_modify['start_line']:
                start_col_offset += len(replacement.split('\n')[-1]) - old_span['end_col']
                # The only case in which we'd offset the end column is if the span_to_modify is a single line
                # and the old span also finishes on the same line.
                # [old_span
                #  ...] ... [span_to_modify]\n"
                if span_to_modify['start_line'] == span_to_modify['end_line']:
                    end_col_offset += len(replacement.split('\n')[-1]) - old_span['end_col']
        
        # We also need to handle the case where the old_span is within the span_to_modify
        elif ((old_span['start_line'] > span_to_modify['start_line']) or \
            ((old_span['start_line'] == span_to_modify['start_line']) and (old_span['start_col'] >= span_to_modify['start_col']))) and \
                ((old_span['end_line'] < span_to_modify['end_line']) or \
                    ((old_span['end_line'] == span_to_modify['end_line']) and (old_span['end_col'] <= span_to_modify['end_col']))):
            
            internal = True
            end_line_offset += replacement.count('\n') - (old_span['end_line'] - old_span['start_line'])
            # Check if this old span ends exactly on the end line of the span_to_modify
            # [span_to_modify
            #  ...
            # [old_span...
            #  ...] ...]
            if old_span['end_line'] == span_to_modify['end_line']:
                last_line = replacement.split('\n')[-1]
                # Question is what to do if the replacement ends with '\n'
                if last_line == '':
                    end_col_offset += len(replacement.splitlines(True)[-1]) - old_span['end_col']
                    end_line_offset -= 1
                else:
                    end_col_offset += len(last_line) - old_span['end_col']
        
        else:
            # The old span must be after the span_to_modify
            # It cannot overlap partially, or be surrounding the span_to_modify
            if not( (old_span['start_line'] > span_to_modify['end_line']) or\
                  (old_span['start_line'] == span_to_modify['end_line'] and old_span['start_col'] > span_to_modify['end_col'])):
                prRed(f"Failed to modify {span_text} because this span is inside a span that changed.")
                return span_text # This is kind of a dealbreaker so we can't proceed
    
    new_start_line = span_to_modify['start_line'] + start_line_offset
    new_end_line = span_to_modify['end_line'] + start_line_offset + end_line_offset
    new_start_col = span_to_modify['start_col'] + start_col_offset
    new_end_col = span_to_modify['end_col'] + end_col_offset

    if any(x <= 0 for x in [new_start_line, new_start_col, new_end_line, new_end_col]):
        prRed(f"Failed to modify {span_text}! Ended up with negative indices")
        return span_text # This is kind of a dealbreaker so we can't proceed

    new_span = f"{span_to_modify['file_name']}:{new_start_line}:{new_start_col}: {new_end_line}:{new_end_col} {span_to_modify['appendage']}"

    # This part is all just verifying that it worked correctly
    old_text = span_to_text(code_dir, span_text, old=True)
    new_text = span_to_text(code_dir, new_span)
    if not internal:
        if old_text != new_text:
            prRed(f"Failed to modify {span_text}! Text mismatch")
    else:
        if span_to_modify in old_spans:
            ind = old_spans.index(span_to_modify)
            replacement = replacements[ind]
            if new_text != replacement:
                prRed(f"Failed to modify {span_text}! Text mismatch")

    prCyan("{} -> {}".format(span_text, new_span))

    return new_span
    

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Translate code snippets to idiomatic Rust')
    parser.add_argument('--code_dir', type=str, required=True, help='Root directory of the code to translate')
    parser.add_argument('--test_dir', type=str, default="", help='Directory containing tests')
    parser.add_argument('--randomize', action='store_true', help='Randomize the order of functions to translate')
    parser.add_argument('--no_chunking', action='store_true', help='Do not chunk the functions into smaller pieces')
    args = parser.parse_args()

    code_dir = args.code_dir.rstrip('/')
    prCyan("Translating code in directory: {}".format(code_dir))
    compile(code_dir)
    selected_tests = select_tests(code_dir, args.test_dir)
    prCyan("Selected tests: {}".format(selected_tests))
    prCyan("Creating a WIP directory which is a copy of the original")
    # Copy this directory to a new directory having the original name with "_WIP" appended to it
    # If it already exists, delete it
    new_dirname = code_dir + '_WIP'
    if args.randomize:
        new_dirname += '_randomized'
    if args.no_chunking:
        new_dirname += '_nochunking'
    if os.path.exists(new_dirname):
        # shutil.rmtree(new_dirname)
        shutil.move(new_dirname, new_dirname + '_old')
    shutil.copytree(code_dir, new_dirname)
    code_dir = new_dirname
    prCyan("Now working in {}".format(code_dir))

    model = get_model_from_name('gpt4o-mini')

    if args.no_chunking:
        run_slicer(code_dir, 'slices.json', max_lines=100000)
    else:
        run_slicer(code_dir, 'slices.json')
    functions = json.loads(Path(code_dir, 'slices.json').read_text())

    # Build call graph of functions
    call_graph = nx.DiGraph()
    for func in functions:
        call_graph.add_node('"{}"'.format(func['func_defid']))
        for call in func['calls']:
            call_graph.add_edge('"{}"'.format(call['caller']), '"{}"'.format(func['func_defid']))

    nx.drawing.nx_pydot.write_dot(call_graph, Path(code_dir, 'callgraph.dot'))
    try:
        run('dot -Tpdf {} -o {}'.format(Path(code_dir, 'callgraph.dot'), Path(code_dir, 'callgraph.pdf')))
    except:
        prRed('Warning - failed to generate callgraph PDF')
        pass
    
    func_ordering = []
    for component in nx.weakly_connected_components(call_graph):
        subgraph = call_graph.subgraph(component)
        try:
            func_ordering += list(reversed(list(nx.topological_sort(subgraph))))
        except nx.NetworkXUnfeasible:
            func_ordering += list(nx.dfs_postorder_nodes(subgraph))
    
    func_ordering = [f.strip('"') for f in func_ordering]
    
    if args.randomize:
        random.shuffle(func_ordering)
    
    # Write the function ordering to a file ordering.txt
    with open(Path(code_dir, 'ordering.txt'), 'w') as f:
        f.write('\n'.join(func_ordering))
    
    # Make a list of all defid and chunk_id
    pair_list = []

    for defid in func_ordering:
        func = [f for f in functions if f['func_defid'] == defid][0]
        # Build a tree of the chunks with sub-chunk relationships
        G = nx.DiGraph()
        G.add_node('root')
        for sub_chunk_num in func['sub_chunks']:
            G.add_edge('root', sub_chunk_num)
        for chunk in func['chunks']:
            G.add_node(chunk['chunk_id'])
            for sub_chunk_num in chunk['sub_chunks']:
                G.add_edge(chunk['chunk_id'], sub_chunk_num)
        # Get the chunks in a post-order traversal
        chunks = list(nx.dfs_postorder_nodes(G, source='root'))
        if args.randomize:
            random.shuffle(chunks)
        pair_list += [(defid, chunk_id) for chunk_id in chunks]
    
    log_file = Path(code_dir, 'log.txt')
    prompt_file = Path(code_dir, 'prompts.txt')

    for defid, chunk_id in pair_list:
        # Reload the slices.json file
        functions = json.loads(Path(code_dir, 'slices.json').read_text())
        func = [f for f in functions if f['func_defid'] == defid][0]
        func = preprocess_func(func)

        prCyan("Translating {}".format(defid))
        if chunk_id == 'root':
            prCyan("Translating the whole function")
            chunk = func
            prompt = construct_prompt_for_func(func)
        else:
            prCyan(f"Translating Chunk {chunk_id}")
            chunk = [c for c in func['chunks'] if c['chunk_id'] == chunk_id][0]
            chunk['calls'] = [] # We need this to make it consistent with func
            prompt = construct_prompt_for_chunk(chunk, func['globals'], func['imports'])
    
        prLightGray(prompt)

        with open(prompt_file, 'a') as f:
            f.write(f"{func['func_defid']}\n")
            f.write(f"Chunk {chunk_id}\n")
            f.write(prompt)
            f.write("----------------------------------------------------------------\n")

        conversation = [{'role': 'system', 'content': 'You are an intelligent code assistant'},
                            {'role': 'user', 'content': prompt.strip()}]
        conversation_start = conversation.copy()

        MAX_ATTEMPTS = 5
        success = False
        num_attempts = 0

        for attempt in range(MAX_ATTEMPTS):

            prCyan(f"Attempt {attempt+1} of {MAX_ATTEMPTS}")
            num_attempts += 1

            try:
                response = model.gen(conversation)[0]
            except ModelException as e:
                prCyan("Model exception")
                prCyan(e)
                prCyan("Trying again")
                continue

            prCyan("Generated translations.")
            print("--------------")
            print(response)
            print("--------------")

            # Parse the response and extract the text between either <FUNC>...</FUNC>, <IMPORT>...</IMPORT> or <CALL>...</CALL> tags
            # Note that there can be multiple <CALL>...</CALL> tags in the response
            if '<IMPORTS>\n' in response:
                imports = response.split('<IMPORTS>\n')[1].split('</IMPORTS>')[0]
            else:
                imports = ''
            
            if '<FUNC>\n' not in response:
                prRed("Response does not contain <FUNC> tag. Trying again.")
                continue
            function_trans = response.split('<FUNC>\n')[1].split('</FUNC>')[0]
            response_parts = response.split('<CALL>\n')[1:]
            call_trans = [r.split('\n</CALL>')[0] for r in response_parts]
            piece_trans = [f.split('/* <CHUNK>')[0] for f in function_trans.split('</CHUNK> */')]
            
            if len(piece_trans) != len(chunk['pieces']) or \
                    len(call_trans) != len(chunk['calls']):
                prRed("Number of pieces or calls does not match. Trying again.")
                prRed(f"Expected {len(chunk['pieces'])} pieces and {len(chunk['calls'])} calls.")
                prRed(f"Got {len(piece_trans)} pieces and {len(call_trans)} calls.")
                continue

            spans = chunk['pieces'] + [c['span'] for c in chunk['calls']]

            prCyan("Replacing spans with translations.")
            replace(code_dir, spans, piece_trans + call_trans, imports)

            prCyan("Attempting to compile the new code")

            try:
                compile(code_dir)
                prGreen("Compilation successful.")
                success = True
            except CompileException as e:
                prRed("Compilation failed.")
                # prCyan(e)
                if "Timeout" in str(e):
                    prRed("Timeout. Trying again.")
                    continue
                prRed("Giving feedback and trying again")
                
                prompt = ("The translation generated the following compile error:\n"
                            f"{e}\n"
                            f"Please re-generate the translation of the code{" and call-sites" if len(chunk['calls']) > 0 else ""}. "
                            f"Remember to follow the same format with <FUNC></FUNC>{", <CALL></CALL>" if len(chunk['calls']) > 0 else ""} and <IMPORTS></IMPORTS> tags (if any). "
                            "DO NOT include markdown characters like '```' or '```rust' in your translation.")
                prLightGray(prompt)
                conversation += [{'role': 'assistant', 'content': response}]
                # Changed this to give feedback only from the last attempt instead of all attempts
                # conversation = conversation_start + [{'role': 'assistant', 'content': response}]
                conversation += [{'role': 'user', 'content': prompt.strip()}]
            
            if success:
                if len(selected_tests) == 0:
                    break
                # Compilation worked. Now try tests
                prCyan("Running tests")
                cwd = os.getcwd()
                binary_path = os.path.join(code_dir, f'target/debug')
                cmd = ' && '.join(f'{os.path.join(args.test_dir, test_name)} {binary_path}' for test_name in selected_tests)
                try:
                    run(cmd)
                    prGreen("Tests passed")
                    break
                except RuntimeError as e:
                    success = False
                    prRed(f"Test failed with error {e}")
                    if "Timeout" in str(e):
                        error_string = "The translated code caused the program to hang."
                    elif "Segmentation Fault" in str(e) or "core dumped" in str(e):
                        error_string = "The translated code caused a segmentation fault."
                    else:
                        error_string = f"The translated code is not exactly equivalent to the original function. It failed the tests."
                    prompt = (f"{error_string}\n"
                                f"Please re-generate the translation of the functions{" and call-sites" if len(func['calls']) > 0 else ""}. "
                                f"Remember to follow the same format with <FUNC></FUNC>{", <CALL></CALL>" if len(func['calls']) > 0 else ""} and <IMPORTS></IMPORTS> tags (if any). "
                                "DO NOT include markdown characters like '```'' or '```rust' in your translation.")
                    prLightGray(prompt)
                    conversation += [{'role': 'assistant', 'content': response}]
                    conversation += [{'role': 'user', 'content': prompt.strip()}]

        if success:
            # After every successful function translation, we have to first modify all the spans because the code has changed
            functions = json.loads(Path(code_dir, 'slices.json').read_text())
            prCyan("Changing spans in slices.json")
            for f_num, f in enumerate(functions):
                functions[f_num]['span'] = modify_span(f['span'], spans, piece_trans + call_trans, imports)
                for i, span in enumerate(f['pieces']):
                    functions[f_num]['pieces'][i] = modify_span(span, spans, piece_trans + call_trans, imports)
                for i, chunk in enumerate(f['chunks']):
                    functions[f_num]['chunks'][i]['span'] = modify_span(chunk['span'], spans, piece_trans + call_trans, imports)
                    for j, span in enumerate(chunk['pieces']):
                        functions[f_num]['chunks'][i]['pieces'][j] = modify_span(span, spans, piece_trans + call_trans, imports)
            
            # Write this back to file
            with open(Path(code_dir, 'slices.json'), 'w') as f:
                prCyan("Writing slices.json")
                f.write(json.dumps(functions, indent=4))
            # Then, we have to re-run the dataflow analysis
            run_dataflow(code_dir, 'slices.json')
            # Next time when the loop runs, it will read the new slices.json file
            # Remove all the .old files
            for file in Path(code_dir).rglob('*.old'):
                file.unlink()
            # Cleanup the compilation target directory
            cleanup(code_dir)
            # Write the defid and "Success" to the log file
            with open(log_file, 'a') as f:
                f.write(f"{func['func_defid']} Chunk {chunk_id}, Success, {num_attempts}\n")
        else:
            # If translating this function was unsuccessful, reset all the changes made in these attempts
            # Replace all the ".rs" files with the ".old" files if they exist
            prCyan("Rolling back changes")
            file_list = [Path(code_dir, span.split(':')[0]) for span in spans]
            for file in file_list:
                if file.with_suffix('.old').exists():
                    shutil.copy(file.with_suffix('.old'), file)
                    file.with_suffix('.old').unlink()
            # Cleanup the compilation target directory
            cleanup(code_dir)
            # Write the defid and "Failure" to the log file
            with open(log_file, 'a') as f:
                f.write(f"{func['func_defid']} Chunk {chunk_id}, Failure, {num_attempts}\n")
