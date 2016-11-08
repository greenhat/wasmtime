"""
Cretonne meta language module.

This module provides classes and functions used to describe Cretonne
instructions.
"""
from __future__ import absolute_import
import importlib
from cdsl import camel_case
from cdsl.predicates import And
from cdsl.types import ValueType
from cdsl.typevar import TypeVar
from cdsl.operands import Operand
from cdsl.formats import InstructionFormat

# The typing module is only required by mypy, and we don't use these imports
# outside type comments.
try:
    from typing import Tuple, Union, Any, Iterable, Sequence, TYPE_CHECKING  # noqa
    from cdsl.predicates import Predicate, FieldPredicate  # noqa
    MaybeBoundInst = Union['Instruction', 'BoundInstruction']
    AnyPredicate = Union['Predicate', 'FieldPredicate']
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from cdsl.typevar import TypeVar  # noqa


# Defining instructions.


class InstructionGroup(object):
    """
    Every instruction must belong to exactly one instruction group. A given
    target architecture can support instructions from multiple groups, and it
    does not necessarily support all instructions in a group.

    New instructions are automatically added to the currently open instruction
    group.
    """

    # The currently open instruction group.
    _current = None  # type: InstructionGroup

    def open(self):
        # type: () -> None
        """
        Open this instruction group such that future new instructions are
        added to this group.
        """
        assert InstructionGroup._current is None, (
                "Can't open {} since {} is already open"
                .format(self, InstructionGroup._current))
        InstructionGroup._current = self

    def close(self):
        # type: () -> None
        """
        Close this instruction group. This function should be called before
        opening another instruction group.
        """
        assert InstructionGroup._current is self, (
                "Can't close {}, the open instuction group is {}"
                .format(self, InstructionGroup._current))
        InstructionGroup._current = None

    def __init__(self, name, doc):
        # type: (str, str) -> None
        self.name = name
        self.__doc__ = doc
        self.instructions = []  # type: List[Instruction]
        self.open()

    @staticmethod
    def append(inst):
        # type: (Instruction) -> None
        assert InstructionGroup._current, \
                "Open an instruction group before defining instructions."
        InstructionGroup._current.instructions.append(inst)


class Instruction(object):
    """
    The operands to the instruction are specified as two tuples: ``ins`` and
    ``outs``. Since the Python singleton tuple syntax is a bit awkward, it is
    allowed to specify a singleton as just the operand itself, i.e., `ins=x`
    and `ins=(x,)` are both allowed and mean the same thing.

    :param name: Instruction mnemonic, also becomes opcode name.
    :param doc: Documentation string.
    :param ins: Tuple of input operands. This can be a mix of SSA value
                operands and other operand kinds.
    :param outs: Tuple of output operands. The output operands must be SSA
                values or `variable_args`.
    :param is_terminator: This is a terminator instruction.
    :param is_branch: This is a branch instruction.
    """

    def __init__(self, name, doc, ins=(), outs=(), **kwargs):
        # type: (str, str, Union[Sequence[Operand], Operand], Union[Sequence[Operand], Operand], **Any) -> None # noqa
        self.name = name
        self.camel_name = camel_case(name)
        self.__doc__ = doc
        self.ins = self._to_operand_tuple(ins)
        self.outs = self._to_operand_tuple(outs)
        self.format = InstructionFormat.lookup(self.ins, self.outs)
        # Indexes into outs for value results. Others are `variable_args`.
        self.value_results = tuple(
                i for i, o in enumerate(self.outs) if o.is_value())
        self._verify_polymorphic()
        InstructionGroup.append(self)

    def __str__(self):
        prefix = ', '.join(o.name for o in self.outs)
        if prefix:
            prefix = prefix + ' = '
        suffix = ', '.join(o.name for o in self.ins)
        return '{}{} {}'.format(prefix, self.name, suffix)

    def snake_name(self):
        # type: () -> str
        """
        Get the snake_case name of this instruction.

        Keywords in Rust and Python are altered by appending a '_'
        """
        if self.name == 'return':
            return 'return_'
        else:
            return self.name

    def blurb(self):
        """Get the first line of the doc comment"""
        for line in self.__doc__.split('\n'):
            line = line.strip()
            if line:
                return line
        return ""

    def _verify_polymorphic(self):
        """
        Check if this instruction is polymorphic, and verify its use of type
        variables.
        """
        poly_ins = [
                i for i in self.format.value_operands
                if self.ins[i].typ.free_typevar()]
        poly_outs = [
                i for i, o in enumerate(self.outs)
                if o.typ.free_typevar()]
        self.is_polymorphic = len(poly_ins) > 0 or len(poly_outs) > 0
        if not self.is_polymorphic:
            return

        # Prefer to use the typevar_operand to infer the controlling typevar.
        self.use_typevar_operand = False
        typevar_error = None
        if self.format.typevar_operand is not None:
            try:
                tv = self.ins[self.format.typevar_operand].typ
                if tv is tv.free_typevar():
                    self.other_typevars = self._verify_ctrl_typevar(tv)
                    self.ctrl_typevar = tv
                    self.use_typevar_operand = True
            except RuntimeError as e:
                typevar_error = e

        if not self.use_typevar_operand:
            # The typevar_operand argument doesn't work. Can we infer from the
            # first result instead?
            if len(self.outs) == 0:
                if typevar_error:
                    raise typevar_error
                else:
                    raise RuntimeError(
                            "typevar_operand must be a free type variable")
            tv = self.outs[0].typ
            if tv is not tv.free_typevar():
                raise RuntimeError("first result must be a free type variable")
            self.other_typevars = self._verify_ctrl_typevar(tv)
            self.ctrl_typevar = tv

    def _verify_ctrl_typevar(self, ctrl_typevar):
        """
        Verify that the use of TypeVars is consistent with `ctrl_typevar` as
        the controlling type variable.

        All polymorhic inputs must either be derived from `ctrl_typevar` or be
        independent free type variables only used once.

        All polymorphic results must be derived from `ctrl_typevar`.

        Return list of other type variables used, or raise an error.
        """
        other_tvs = []
        # Check value inputs.
        for opidx in self.format.value_operands:
            typ = self.ins[opidx].typ
            tv = typ.free_typevar()
            # Non-polymorphic or derived form ctrl_typevar is OK.
            if tv is None or tv is ctrl_typevar:
                continue
            # No other derived typevars allowed.
            if typ is not tv:
                raise RuntimeError(
                        "{}: type variable {} must be derived from {}"
                        .format(self.ins[opidx], typ.name, ctrl_typevar))
            # Other free type variables can only be used once each.
            if tv in other_tvs:
                raise RuntimeError(
                        "type variable {} can't be used more than once"
                        .format(tv.name))
            other_tvs.append(tv)

        # Check outputs.
        for result in self.outs:
            typ = result.typ
            tv = typ.free_typevar()
            # Non-polymorphic or derived from ctrl_typevar is OK.
            if tv is None or tv is ctrl_typevar:
                continue
            raise RuntimeError(
                    "type variable in output not derived from ctrl_typevar")

        return other_tvs

    @staticmethod
    def _to_operand_tuple(x):
        # type: (Union[Sequence[Operand], Operand]) -> Tuple[Operand, ...]
        # Allow a single Operand instance instead of the awkward singleton
        # tuple syntax.
        if isinstance(x, Operand):
            x = (x,)
        else:
            x = tuple(x)
        for op in x:
            assert isinstance(op, Operand)
        return x

    def bind(self, *args):
        # type: (*ValueType) -> BoundInstruction
        """
        Bind a polymorphic instruction to a concrete list of type variable
        values.
        """
        assert self.is_polymorphic
        return BoundInstruction(self, args)

    def __getattr__(self, name):
        # type: (str) -> BoundInstruction
        """
        Bind a polymorphic instruction to a single type variable with dot
        syntax:

        >>> iadd.i32
        """
        return self.bind(ValueType.by_name(name))

    def fully_bound(self):
        # type: () -> Tuple[Instruction, Tuple[ValueType, ...]]
        """
        Verify that all typevars have been bound, and return a
        `(inst, typevars)` pair.

        This version in `Instruction` itself allows non-polymorphic
        instructions to duck-type as `BoundInstruction`\s.
        """
        assert not self.is_polymorphic, self
        return (self, ())

    def __call__(self, *args):
        """
        Create an `ast.Apply` AST node representing the application of this
        instruction to the arguments.
        """
        from .ast import Apply
        return Apply(self, args)


class BoundInstruction(object):
    """
    A polymorphic `Instruction` bound to concrete type variables.
    """

    def __init__(self, inst, typevars):
        # type: (Instruction, Tuple[ValueType, ...]) -> None
        self.inst = inst
        self.typevars = typevars
        assert len(typevars) <= 1 + len(inst.other_typevars)

    def __str__(self):
        return '.'.join([self.inst.name, ] + list(map(str, self.typevars)))

    def bind(self, *args):
        # type: (*ValueType) -> BoundInstruction
        """
        Bind additional typevars.
        """
        return BoundInstruction(self.inst, self.typevars + args)

    def __getattr__(self, name):
        # type: (str) -> BoundInstruction
        """
        Bind an additional typevar dot syntax:

        >>> uext.i32.i8
        """
        return self.bind(ValueType.by_name(name))

    def fully_bound(self):
        # type: () -> Tuple[Instruction, Tuple[ValueType, ...]]
        """
        Verify that all typevars have been bound, and return a
        `(inst, typevars)` pair.
        """
        if len(self.typevars) < 1 + len(self.inst.other_typevars):
            unb = ', '.join(
                    str(tv) for tv in
                    self.inst.other_typevars[len(self.typevars) - 1:])
            raise AssertionError("Unbound typevar {} in {}".format(unb, self))
        assert len(self.typevars) == 1 + len(self.inst.other_typevars)
        return (self.inst, self.typevars)

    def __call__(self, *args):
        """
        Create an `ast.Apply` AST node representing the application of this
        instruction to the arguments.
        """
        from .ast import Apply
        return Apply(self, args)


# Defining target ISAs.


class TargetISA(object):
    """
    A target instruction set architecture.

    The `TargetISA` class collects everything known about a target ISA.

    :param name: Short mnemonic name for the ISA.
    :param instruction_groups: List of `InstructionGroup` instances that are
        relevant for this ISA.
    """

    def __init__(self, name, instruction_groups):
        self.name = name
        self.settings = None
        self.instruction_groups = instruction_groups
        self.cpumodes = list()

    def finish(self):
        """
        Finish the definition of a target ISA after adding all CPU modes and
        settings.

        This computes some derived properties that are used in multilple
        places.

        :returns self:
        """
        self._collect_encoding_recipes()
        self._collect_predicates()
        return self

    def _collect_encoding_recipes(self):
        """
        Collect and number all encoding recipes in use.
        """
        self.all_recipes = list()
        rcps = set()
        for cpumode in self.cpumodes:
            for enc in cpumode.encodings:
                recipe = enc.recipe
                if recipe not in rcps:
                    recipe.number = len(rcps)
                    rcps.add(recipe)
                    self.all_recipes.append(recipe)

    def _collect_predicates(self):
        """
        Collect and number all predicates in use.

        Sets `instp.number` for all used instruction predicates and places them
        in `self.all_instps` in numerical order.

        Ensures that all ISA predicates have an assigned bit number in
        `self.settings`.
        """
        self.all_instps = list()
        instps = set()
        for cpumode in self.cpumodes:
            for enc in cpumode.encodings:
                instp = enc.instp
                if instp and instp not in instps:
                    # assign predicate number starting from 0.
                    instp.number = len(instps)
                    instps.add(instp)
                    self.all_instps.append(instp)

                # All referenced ISA predicates must have a number in
                # `self.settings`. This may cause some parent predicates to be
                # replicated here, which is OK.
                if enc.isap:
                    self.settings.number_predicate(enc.isap)


class CPUMode(object):
    """
    A CPU mode determines which instruction encodings are active.

    All instruction encodings are associated with exactly one `CPUMode`, and
    all CPU modes are associated with exactly one `TargetISA`.

    :param name: Short mnemonic name for the CPU mode.
    :param target: Associated `TargetISA`.
    """

    def __init__(self, name, isa):
        self.name = name
        self.isa = isa
        self.encodings = []
        isa.cpumodes.append(self)

    def __str__(self):
        return self.name

    def enc(self, *args, **kwargs):
        """
        Add a new encoding to this CPU mode.

        Arguments are the `Encoding constructor arguments, except for the first
        `CPUMode argument which is implied.
        """
        self.encodings.append(Encoding(self, *args, **kwargs))


class EncRecipe(object):
    """
    A recipe for encoding instructions with a given format.

    Many different instructions can be encoded by the same recipe, but they
    must all have the same instruction format.

    :param name: Short mnemonic name for this recipe.
    :param format: All encoded instructions must have this
            :py:class:`InstructionFormat`.
    """

    def __init__(self, name, format, instp=None, isap=None):
        self.name = name
        self.format = format
        self.instp = instp
        self.isap = isap
        if instp:
            assert instp.predicate_context() == format

    def __str__(self):
        return self.name


class Encoding(object):
    """
    Encoding for a concrete instruction.

    An `Encoding` object ties an instruction opcode with concrete type
    variables together with and encoding recipe and encoding bits.

    :param cpumode: The CPU mode where the encoding is active.
    :param inst: The :py:class:`Instruction` or :py:class:`BoundInstruction`
                 being encoded.
    :param recipe: The :py:class:`EncRecipe` to use.
    :param encbits: Additional encoding bits to be interpreted by `recipe`.
    :param instp: Instruction predicate, or `None`.
    :param isap: ISA predicate, or `None`.
    """

    def __init__(self, cpumode, inst, recipe, encbits, instp=None, isap=None):
        # type: (CPUMode, MaybeBoundInst, EncRecipe, int, AnyPredicate, AnyPredicate) -> None # noqa
        assert isinstance(cpumode, CPUMode)
        assert isinstance(recipe, EncRecipe)
        self.inst, self.typevars = inst.fully_bound()
        self.cpumode = cpumode
        assert self.inst.format == recipe.format, (
                "Format {} must match recipe: {}".format(
                    self.inst.format, recipe.format))
        self.recipe = recipe
        self.encbits = encbits
        # Combine recipe predicates with the manually specified ones.
        self.instp = And.combine(recipe.instp, instp)
        self.isap = And.combine(recipe.isap, isap)

    def __str__(self):
        return '[{}#{:02x}]'.format(self.recipe, self.encbits)

    def ctrl_typevar(self):
        """
        Get the controlling type variable for this encoding or `None`.
        """
        if self.typevars:
            return self.typevars[0]
        else:
            return None


# Import the fixed instruction formats now so they can be added to the
# registry.
importlib.import_module('base.formats')
