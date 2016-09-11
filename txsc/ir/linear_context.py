from collections import defaultdict
import copy

from txsc.symbols import ScopeType, SymbolType
from txsc.transformer import BaseTransformer
from txsc.ir import formats, IRError
from txsc.ir.instructions import LInstructions
from txsc.ir.linear_visitor import LIROptions, BaseLinearVisitor, op_for_int
from txsc.ir.linear_runtime import StackState, get_alt_stack_manager_cls
import txsc.ir.linear_nodes as types

class ConditionalBranch(object):
    """A branch of a conditional.

    Attributes:
        is_truebranch (bool): Whether is represents a branch that runs if the test passes.
        start (int): The index of the first op of this branch.
        end (int): The index of the last op of this branch.
        nest_level (int): The level of nesting that this branch is at.
        orelse (ConditionalBranch): The corresponding conditional branch.

    """
    def __init__(self, is_truebranch=True, start=0, end=0, nest_level=0, orelse=None):
        self.is_truebranch = is_truebranch
        self.start = start
        self.end = end
        self.nest_level = nest_level
        self.orelse = orelse

    def __str__(self):
        return '%s(%s, %s)' % (self.is_truebranch, self.start, self.end)

    def __repr__(self):
        return str(self)

    def is_in_branch(self, idx):
        if idx >= self.start and idx <= self.end:
            return True
        return False

class LinearContextualizer(BaseLinearVisitor):
    """Populates metadata attributes of linear IR instructions."""
    def __init__(self, symbol_table, options=LIROptions()):
        super(LinearContextualizer, self).__init__(symbol_table, options)
        # {assumption_name: [occurrence_index, ...], ...}
        self.assumptions = defaultdict(list)
        # {assumption_name: [operation_index, ...], ...}
        self.duplicated_assumptions = defaultdict(list)
        # {assumption_name: depth, ...}
        self.alt_stack_assumptions = {}
        # {variable_name: [occurrence_index, ...], ...}
        self.assignments = defaultdict(list)
        # {variable_name: [occurrence_index, ...], ...}
        self.variables = defaultdict(list)
        # [ConditionalBranch(), ...]
        self.branches = []
        # Current level of conditional nesting.
        self.current_nest_level = 0

    def log_and_raise(self, err_class, msg):
        """Log an error and raise an exception."""
        self.error(msg)
        raise err_class(msg)

    def find_duplicate_assumptions(self):
        """Find operations which use the same assumption more than once."""
        base = [types.Assumption(), types.Assumption()]
        templates = []
        # Collect opcodes that use the last two stack items as arguments.
        for opcode in types.get_opcodes().values():
            if not issubclass(opcode, types.OpCode) or not opcode.args:
                continue
            if all(arg_idx in opcode.args for arg_idx in [1, 2]):
                templates.append(base + [opcode()])

        for i in range(len(self.instructions)):
            for template in templates:
                if not self.instructions.matches_template(template, i, strict=False):
                    continue
                ops = self.instructions[i:i + len(template)]
                # Continue if the assumptions aren't of the same value.
                if ops[0].var_name != ops[1].var_name:
                    continue
                # Append the negative index of the operation to duplicated_assumptions[assumption_name].
                self.duplicated_assumptions[ops[0].var_name].append(-1 * (len(self.instructions) - ops[-1].idx))

    def is_duplicated_assumption(self, op):
        """Get whether op is in an operation that uses its assumed value twice."""
        for depth in self.duplicated_assumptions[op.var_name]:
            if self.instructions[depth] == self.nextop(op):
                return True
        return False

    def is_declaration(self, op):
        """Get whether op is the first assignment to its symbol."""
        if not isinstance(op, types.Assignment):
            return False
        assignments = self.assignments.get(op.var_name, [])
        return assignments and op.idx == assignments[0]

    def is_before_conditionals(self, idx):
        """Get whether idx is before any conditional branches."""
        if not self.branches or idx < self.branches[0].start:
            return True
        return False

    def is_last_occurrence(self, op):
        """Get whether op is the last occurrence of an Assignment, Assumption, or Variable."""
        if not isinstance(op, (types.Assignment, types.Assumption, types.Variable)):
            raise TypeError('An Assignment, Assumption, or Variable instance is required')
        name = op.var_name
        dicts = {
            'Assignment': self.assignments,
            'Assumption': self.assumptions,
            'Variable': self.variables,
        }
        idxs = dicts[op.__class__.__name__][name]
        return idxs and op.idx == idxs[-1]

    def _is_after_uneven_conditional(self, idx):
        """Get whether idx is after an uneven conditional."""
        branch_deltas = {True: [], False: []}
        idx_branch = self.get_branch(idx)
        for branch in self.branches:
            if branch == idx_branch:
                continue
            elif branch.end < idx and (not idx_branch or branch != idx_branch.orelse):
                # Sum the deltas of conditional branches before idx.
                branch_deltas[branch.is_truebranch].append(sum(i.delta for i in self.instructions[branch.start:branch.end+1]))
        return not sum(branch_deltas[True]) == sum(branch_deltas[False])

    def get_branch(self, idx):
        """Get the branch that idx is in."""
        idx_branch = None
        # Find the branch that idx is in.
        for branch in self.branches:
            if branch.is_in_branch(idx):
                if not idx_branch or idx_branch.nest_level < branch.nest_level:
                    idx_branch = branch
        return idx_branch

    def find_alt_stack_assumptions(self):
        """Find the assumptions that are accessed with the alt stack.

        These assumptions are those that are used after uneven conditionals.
        """
        names = {}
        for name, idxs in self.assumptions.items():
            if any(self._is_after_uneven_conditional(idx) for idx in idxs):
                if not self.options.use_altstack_for_assumptions:
                    self.log_and_raise(IRError, 'Assumption encountered after uneven conditional')
                symbol = self.symbol_table.lookup(name).value
                names[name] = symbol.depth
                continue
        self.alt_stack_assumptions = names

    def uses_alt_stack(self, assumption_name):
        """Get whether assumption_name is accessed via the alt stack."""
        if not self.options.use_altstack_for_assumptions:
            return False
        return assumption_name in self.alt_stack_assumptions.keys()

    def following_occurrences(self, assumption_name, idx):
        """Get the indices of occurrences of assumption_name after idx."""
        branches = list(self.branches)
        match_any_branch = False
        if self.is_before_conditionals(idx):
            match_any_branch = True

        assumptions = self.assumptions[assumption_name]
        following = []

        idx_branch = self.get_branch(idx)
        for assumption_idx in assumptions:
            if assumption_idx <= idx:
                continue
            if not branches:
                following.append(assumption_idx)
            else:
                for branch in branches:
                    if not match_any_branch and branch.is_in_branch(idx):
                        continue
                    if branch.end > idx and (not idx_branch or branch != idx_branch.orelse):
                        following.append(assumption_idx)

                # Assumption beyond a conditional.
                if assumption_idx > branches[-1].end:
                    following.append(assumption_idx)

        return following

    def nextop(self, op):
        """Get the operation that follows op."""
        try:
            return self.instructions[op.idx + 1]
        except IndexError:
            return None

    def total_delta(self, idx):
        """Get the total delta of script operations before idx."""
        total = 0
        # Add 1 for every assumed stack item.
        total += len(self.symbol_table.lookup('_stack_names').value)
        branches = self.branches
        if self.is_before_conditionals(idx):
            total += sum(i.delta for i in self.instructions[:idx])
            return total

        idx_branch = self.get_branch(idx)

        branch_ranges = []
        branch_deltas = {True: [], False: []}
        for branch in branches:
            branch_ranges.append((branch.start, branch.end))
            if branch == idx_branch:
                # Add the deltas of instructions before idx in its branch.
                total += sum(i.delta for i in self.instructions[branch.start:idx])
            elif branch.end < idx and (not idx_branch or branch != idx_branch.orelse):
                # Sum the deltas of conditional branches before idx.
                branch_deltas[branch.is_truebranch].append(sum(i.delta for i in self.instructions[branch.start:branch.end+1]))

        # Add the deltas from conditional branches before idx.
        # It doesn't matter which is_truebranch value we use here,
        # since assumptions after uneven conditionals are not handled this way.
        total += sum(branch_deltas[True])

        # Add the deltas from operations outside of branches before idx.
        for instruction in self.instructions[:idx]:
            if not any(instruction.idx >= start and instruction.idx <= end for start,end in branch_ranges):
                total += instruction.delta

        return total

    def get_last_branch(self):
        """Get the last conditional branch with the current nest level."""
        for branch in reversed(self.branches):
            if branch.nest_level == self.current_nest_level:
                return branch

    def contextualize(self, instructions):
        """Perform contextualization on instructions.

        Most of these calculations will only succeed if no script execution
        must be done to place the necessary arguments into position on the stack.
        """
        if not isinstance(instructions, LInstructions):
            raise TypeError('A LInstructions instance is required')
        self.assumptions.clear()
        self.assignments.clear()
        self.variables.clear()
        self.branches = []
        self.instructions = instructions

        for i, instruction in enumerate(iter(instructions)):
            instruction.idx = i
            self.visit(instruction)

        # If the current nest level is greater than 0,
        # then the script ended within a conditional branch.
        if self.current_nest_level > 0:
            self.log_and_raise(IRError, 'Script ended without ending all conditionals')

        # Validate arguments for certain opcodes.
        if not self.options.allow_invalid_comparisons:
            for instruction in self.instructions:
                if isinstance(instruction, (types.Hash160, types.RipeMD160)):
                    self.check_Hash160(instruction)
                elif isinstance(instruction, (types.Hash256, types.Sha256)):
                    self.check_Hash256(instruction)

    def check_Hash160(self, op):
        """Check that 20-byte pushes are used as RIPEMD-160 hashes."""
        data_push = self.nextop(op)
        if not isinstance(data_push, types.Push):
            return
        opcode = self.nextop(data_push)

        if not isinstance(opcode, (types.Equal, types.EqualVerify)):
            return
        if len(data_push.data) != 20:
            self.log_and_raise(IRError, 'Non-hash160 compared to the result of %s' % op.name)

    def check_Hash256(self, op):
        """Check that 32-byte pushes are used as SHA256 hashes."""
        data_push = self.nextop(op)
        if not isinstance(data_push, types.Push):
            return
        opcode = self.nextop(data_push)

        if not isinstance(opcode, (types.Equal, types.EqualVerify)):
            return
        if len(data_push.data) != 32:
            self.log_and_raise(IRError, 'Non-hash256 compared to the result of %s' % op.name)

    def visit(self, instruction):
        method = getattr(self, 'visit_%s' % instruction.__class__.__name__, None)
        if not method:
            return
        return method(instruction)

    def visit_Assignment(self, op):
        self.assignments[op.var_name].append(op.idx)

    def visit_Variable(self, op):
        self.variables[op.var_name].append(op.idx)

    def visit_Assumption(self, op):
        self.assumptions[op.var_name].append(op.idx)

    def visit_If(self, op):
        self.current_nest_level += 1
        self.branches.append(ConditionalBranch(is_truebranch = True, start = op.idx + 1, nest_level = self.current_nest_level))

    def visit_NotIf(self, op):
        self.current_nest_level += 1
        self.branches.append(ConditionalBranch(is_truebranch = False, start = op.idx + 1, nest_level = self.current_nest_level))

    def visit_Else(self, op):
        last_branch = self.get_last_branch()
        if not last_branch:
            self.log_and_raise(IRError, 'Else statement requires a preceding If or NotIf statement')
        last_branch.end = op.idx - 1

        new_branch = ConditionalBranch(is_truebranch = not last_branch.is_truebranch, start = op.idx + 1, nest_level = self.current_nest_level, orelse = last_branch)
        self.branches.append(new_branch)
        # Assign this branch to the orelse attribute of the preceding statement.
        last_branch.orelse = new_branch

    def visit_EndIf(self, op):
        last_branch = self.get_last_branch()
        if not last_branch:
            self.log_and_raise(IRError, 'EndIf encountered with no preceding conditional')
        last_branch.end = op.idx - 1
        self.current_nest_level -= 1

    def visit_CheckMultiSig(self, op):
        """Attempt to determine opcode arguments."""
        i = 1
        num_pubkeys = LInstructions.instruction_to_int(self.instructions[op.idx - i])
        if num_pubkeys is None:
            return

        i += 1
        i += num_pubkeys
        num_sigs = LInstructions.instruction_to_int(self.instructions[op.idx - i])
        if num_sigs is None:
            return

        i += 1
        i += num_sigs

        op.num_pubkeys = num_pubkeys
        op.num_sigs = num_sigs
        op.args = range(i)

    def visit_CheckMultiSigVerify(self, op):
        return self.visit_CheckMultiSig(op)

    def visit_IfDup(self, op):
        """Attempt to determine opcode's delta."""
        arg = LInstructions.instruction_to_int(self.instructions[op.idx - 1])
        if arg is None:
            return

        op.delta = 1 if arg else 0

    def visit_Pick(self, op):
        """Attempt to determine opcode argument."""
        arg = LInstructions.instruction_to_int(self.instructions[op.idx - 1])
        if arg is None:
            return

        op.args = [1, arg + 2]

    def visit_Roll(self, op):
        """Attempt to determine opcode argument."""
        return self.visit_Pick(op)

    def visit_FunctionCall(self, op):
        for i in op.ops:
            i.idx = op.idx

class LinearInliner(BaseLinearVisitor):
    """Replaces variables with stack operations."""
    def __init__(self, symbol_table, options=LIROptions()):
        super(LinearInliner, self).__init__(symbol_table, options)
        self.contextualizer = LinearContextualizer(symbol_table, options)
        self.stack = StackState(symbol_table)
        self.alt_stack_manager = get_alt_stack_manager_cls()(options)

    def inline(self, instructions, peephole_optimizer):
        """Perform inlining of variables in instructions.

        Inlining is performed by iterating through each instruction and
        calling visitor methods. If no result is returned, the next
        instruction is visited.

        If there is a result, the instruction is replaced with that result,
        and the iteration begins again.

        Inlining ends when all instructions have been iterated over without
        any result.
        """
        if not isinstance(instructions, LInstructions):
            raise TypeError('A LInstructions instance is required')
        self.instructions = instructions
        self.stack.clear(clear_assumptions=True)
        self.alt_stack_manager.clear()

        stack_names = self.symbol_table.lookup('_stack_names')
        stack_names = stack_names.value if stack_names else []
        self.stack.add_stack_assumptions([types.Assumption(var_name) for var_name in stack_names])

        # Find operations that use the same assumed stack item more than once.
        self.contextualizer.contextualize(instructions)
        self.contextualizer.find_duplicate_assumptions()
        self.contextualizer.find_alt_stack_assumptions()

        # Prepend the operations that set up the alt stack variables.
        initial_ops = self.alt_stack_manager.analyze(instructions, self.contextualizer.alt_stack_assumptions)
        instructions.insert_slice(0, initial_ops)

        # Loop until no inlining can be done.
        while 1:
            self.stack.clear(clear_assumptions=False)
            peephole_optimizer.optimize(instructions)
            self.contextualizer.contextualize(instructions)
            inlined = False

            for i, node in enumerate(instructions):
                result = self.visit(node)
                if result is None:
                    continue
                if not isinstance(result, list):
                    result = [result]

                instructions.replace_slice(i, i+1, result)
                inlined = True
                break

            if not inlined:
                break

    def visit_consecutive_assumptions(self, assumptions):
        """Handle a row of consecutive assumptions."""
        # If the assumptions are all being ROLL'd to the top, the final assumption's
        # OP_ROLL argument is 0, and the others are sequential, then nothing needs to be done.
        assumption_ops = map(self.bring_assumption_to_top, assumptions)
        if all(len(ops) == 2 and isinstance(ops[1], types.Roll) for ops in assumption_ops):
            roll_args = [ops[0] for ops in assumption_ops]
            iterator = (LInstructions.instruction_to_int(i) for i in reversed(roll_args))
            # Ensure that the value is 0 (instead of None).
            # http://stackoverflow.com/questions/28885455/python-check-whether-list-is-sequential-or-not
            if next(iterator) == 0 and all(a == b for (a, b) in enumerate(iterator, 1)):
                return []

    def bring_assumption_to_top(self, op):
        symbol = self.symbol_table.lookup(op.var_name)
        total_delta = self.contextualizer.total_delta(op.idx)

        arg = max(0, total_delta - symbol.value.height - 1)
        highest, highest_stack_idx = self.stack.get_highest_assumption(op)
        if highest is not None:
            arg = total_delta - highest_stack_idx - 1

        arg = op_for_int(arg)
        opcode = self.get_opcode_for_assumption(op)
        return [arg, opcode]

    def get_opcode_for_assumption(self, op):
        """Get the opcode to use when bringing op to the top of the stack."""
        # Use OP_PICK if there are other occurrences after this one,
        # or if the same assumed item is used more than once in an operation.
        opcode = types.Roll
        following = self.contextualizer.following_occurrences(op.var_name, op.idx)
        if following:
            # Don't change the opcode if the only following occurrence is a duplicated assumption.
            nextop = self.contextualizer.nextop(op)
            # If more than one occurrence follows, use OP_PICK.
            if len(following) > 1:
                opcode = types.Pick
            elif nextop.idx not in following or not self.contextualizer.is_duplicated_assumption(nextop):
                opcode = types.Pick
        if self.contextualizer.is_duplicated_assumption(op):
            opcode = types.Pick
        return opcode()

    def visit(self, instruction):
        method = getattr(self, 'visit_%s' % instruction.__class__.__name__, None)
        if not method:
            return
        return method(instruction)

    def visit_Assumption(self, op):
        self.stack.clear(clear_assumptions=False)
        self.stack.process_instructions(self.instructions[:op.idx])
        # Use the alt stack if this is an alt stack assumption.
        if self.contextualizer.uses_alt_stack(op.var_name):
            return self.alt_stack_manager.get_variable(op, self.contextualizer.is_last_occurrence(op))
        # Detect whether there are multiple assumptions in a row.
        assumptions = [op]
        symbols = [self.symbol_table.lookup(op.var_name)]
        while 1:
            nextop = self.contextualizer.nextop(assumptions[-1])
            if nextop.__class__ is not types.Assumption:
                break
            symbol = self.symbol_table.lookup(nextop.var_name)
            if symbol.value.depth != symbols[-1].value.depth - 1:
                break

            assumptions.append(nextop)
            symbols.append(symbol)

        # Only account for consecutive assumptions if none of them
        # are accessed using the alt stack.
        names = [i.var_name for i in assumptions]
        if len(assumptions) > 1 and not any(self.contextualizer.uses_alt_stack(name) for name in names):
            result = self.visit_consecutive_assumptions(assumptions)
            if result is not None:
                return result

        # If there are no consecutive assumptions, use opcodes to bring this assumption to the top.
        return self.bring_assumption_to_top(op)

    def visit_Assignment(self, op):
        if not self.contextualizer.is_declaration(op):
            return self.alt_stack_manager.set_variable(op)

    def visit_Variable(self, op):
        result = self.alt_stack_manager.get_variable(op, self.contextualizer.is_last_occurrence(op))
        # If None is returned, calculate the variable's value using
        # the runtime StackState.
        if result is None:
            self.stack.clear(clear_assumptions=False)
            self.stack.process_instructions(self.instructions[:op.idx])
            result = self.symbol_table.lookup(op.var_name).value
        return result

    def visit_InnerScript(self, op):
        result = self.map_visit(op.ops)

        changed = False
        new_ops = []
        for i, node in enumerate(result):
            if node is None:
                new_ops.append(op.ops[i])
                continue
            changed = True
            new = node
            if isinstance(node, list):
                new = node[0]
            new_ops.append(new)

        if not changed:
            return None
        op.ops = new_ops
        return op

    def visit_FunctionCall(self, op):
        self.stack.clear(clear_assumptions=False)
        self.stack.process_instructions(self.instructions[:op.idx])

        func = self.symbol_table.lookup(op.func_name).value
        self.symbol_table.begin_scope(scope_type=ScopeType.Function)
        # Bind args to func's formal parameters.
        for param, arg in zip(func.args, op.args):
            self.symbol_table.add_symbol(name=param.id, value=arg, type_=SymbolType.FuncArg, declaration=True)

        # Visit the function ops.
        ret = []
        for i in op.ops:
            result = self.visit(i)
            # Return the op if it's unchanged.
            if result is None:
                ret.append(i)
            elif isinstance(result, list):
                ret.extend(result)
            else:
                ret.append(result)

        self.symbol_table.end_scope()

        return ret
