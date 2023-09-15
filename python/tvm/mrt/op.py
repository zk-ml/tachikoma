""" op helper function

This module should not be imported with *, since this
    overrides many common builtins' type.
"""

from dataclasses import dataclass

from .opns import *
from .utils import *
from .symbol import *

def subgraph(symbol: Symbol, inames=[], onames=[]):
    out = []
    def _find(sym: Symbol):
        if sym.name in inames:
            return as_variable(sym)
        elif sym.name in onames:
            out.append(sym)

    def_out = transform(symbol, _find)
    out = out or [ def_out, ]
    out = out[0] if len(out) else tuple(*out)
    return out

def variable(name, shape, dtype) -> Symbol:
    """ Create varible for symbol. """
    return Symbol.from_dict({},
            name=name, op_name = VAR,
            args = [], extra_attrs = {
                "shape": shape,
                "dtype": dtype, })

def as_variable(symbol: Symbol) -> Symbol:
    return symbol.copy(op_name=VAR, args=[], attrs={
        "shape": symbol.shape,
        "dtype": symbol.dtype,
        })

def retrieve_operator(symbol: Symbol) -> Symbol:
    return symbol.copy(args=[as_variable(c) for c in symbol.args])

def infer_type(symbol: Symbol) -> Symbol:
    from tvm import relay, ir
    from tvm.mrt import sym_expr

    expr = sym_expr.symbol2expr(symbol)
    mod = relay.transform.InferType()(ir.IRModule.from_expr(expr))
    expr = mod["main"].body
    return sym_expr.expr2symbol(expr)

def graph_like(new: Symbol, old: Symbol) -> Symbol:
    old_sym_iter = iter(sym2list(old))
    def _sym_like(sym: Symbol):
        target = next(old_sym_iter)
        assert target.op_name == sym.op_name
        sym.attrs.update({
            k: v for k, v in target.attrs.items() \
                if k not in sym.attrs })
        sym.extra_attrs.update({
            k: v for k, v in target.extra_attrs.items() \
                if k not in sym.extra_attrs })
        return sym.like(target)
    return transform(new, _sym_like)

@dataclass(repr=False)
class InferType(Symbol):
    def __call__(self):
        assert is_operator(self)

        if type(self) is InferType:
            sym = retrieve_operator(self)
            sym = infer_type(sym)
            self.shape = sym.shape
            self.dtype = sym.dtype
        else:
            self.shape = self._infer_shape()
            self.dtype = self._infer_type()
        return self

    def _infer_type(self):
        assert all([self.args[0].dtype == a.dtype \
                for a in self.args])
        return self.args[0].dtype

    def _infer_shape(self) -> ShapeT:
        raise NotImplementedError("")

@dataclass(repr=False)
class FirstLikeInferType(InferType):
    def _infer_shape(self) -> ShapeT:
        return self.args[0].shape
def _new_op(op_name, *args,
        extra_attrs=None, **attrs) -> Symbol:
    return Symbol.from_dict({},
            name=N.n(), op_name=op_name,
            args=args or [], attrs=attrs or {},
            extra_attrs=extra_attrs or {})

def _register_op(
        op_name,
        infer_type: typing.Type[InferType] = InferType):
    def _op(*args, **attrs) -> Symbol:
        op = _new_op(op_name, *args, **attrs)
        return infer_type.base(op)()
    return _op

Tuple = _register_op(TUPLE)
TupleGetItem = _register_op(TUPLE_GET_ITEM)

nn_conv2d = _register_op(CONV2D)
nn_dense = _register_op(DENSE)
nn_batch_norm = _register_op(BATCH_NORM)
bias_add = _register_op(BIAS_ADD)

sum = _register_op(SUM)
#  mean = _register_op(MEAN)
clip = _register_op(CLIP)
ceil = _register_op(CEIL)
right_shift = _register_op(RIGHT_SHIFT)
# astype = _register_op(AS_TYPE)
cast = _register_op(CAST)
#  flatten = _register_op(FLATTEN)

repeat = _register_op(REPEAT)
reshape = _register_op(RESHAPE)

add = _register_op(ADD)
sub = _register_op(SUB)
mul = _register_op(MUL)

requant = _register_op(REQUANT, FirstLikeInferType)
pclip = _register_op(PCLIP, FirstLikeInferType)
rs_pclip = _register_op(RS_PCLIP, FirstLikeInferType)

def is_operator(symbol: Symbol, params: ParametersT = {}):
    return symbol.op_name != VAR
def is_variable(symbol: Symbol, params: ParametersT = {}):
    return symbol.op_name == VAR
def is_input(symbol: Symbol, params: ParametersT):
    return is_variable(symbol) and symbol.name not in params
def is_param(symbol: Symbol, params: ParametersT):
    return is_variable(symbol) and symbol.name in params

