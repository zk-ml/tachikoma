import os
from os import path

ROOT = path.dirname(__file__)
os.sys.path.insert(0, path.join(ROOT, "python"))

import tvm
from tvm import relay, ir
from tvm.relay import testing
from tvm.mrt.utils import *

from tvm.mrt import runtime
from tvm.mrt import stats, dataset
from tvm.mrt import utils

import sys
import numpy as np
from typing import Tuple

from PIL import Image
from tvm.contrib.download import download_testdata
def get_real_image(im_height, im_width) -> np.ndarray:
    repo_base = "https://github.com/dmlc/web-data/raw/main/tensorflow/models/InceptionV1/"
    img_name = "elephant-299.jpg"
    image_url = path.join(repo_base, img_name)
    img_path = download_testdata(image_url, img_name, module="data")
    image = Image.open(img_path).resize((im_height, im_width))
    data = np.array(image).astype("float32")
    data = np.reshape(data, (1, im_height, im_width, 3))
    data = np.transpose(data, (0, 3, 1, 2))
    data = data / 255.0
    return data

batch_size = 16

def load_model_from_mx() -> Tuple[ir.IRModule, ParametersT]:
    import mxnet as mx
    from mrt import gluon_zoo as gluon
    spath, ppath = gluon.save_model("resnet18_v1", ctx=mx.cpu())
    print(spath, ppath)
    symbol, params = gluon.load_model(spath, ppath)
    return relay.frontend.from_mxnet(symbol, arg_params=params)



if False:
    num_class = 10
    image_shape = (1, 28, 28)
    mod, params = testing.mlp.get_workload(
            num_classes=num_class,
            image_shape=image_shape,
            batch_size=batch_size)
else:
    num_class = 1000
    image_shape = (3, 224, 224)
    out_shape = (batch_size, num_class)
    #  mod, params = load_model_from_mx()
    #  mod, params = testing.resnet.get_workload(
    #          batch_size=batch_size,
    #          num_classes=num_class,
    #          num_layers=18,
    #          image_shape=image_shape,)

data_shape = (batch_size,) + image_shape

def load_model_from_torch() -> Tuple[ir.IRModule, ParametersT]:
    import torch
    from torchvision import models

    weights = models.ResNet18_Weights.IMAGENET1K_V1
    model = models.resnet18(weights=weights)
    model = model.eval()
    input_data = torch.randn(data_shape)
    script_module = torch.jit.trace(model, [input_data]).eval()
    return relay.frontend.from_pytorch(
            script_module, [ ("input", data_shape) ])

mod, params = load_model_from_torch()

mod: tvm.IRModule = mod
func: relay.function.Function = mod["main"]
expr: ir.RelayExpr = func.body

from tvm.mrt.trace import Trace
from tvm.mrt.opns import *
from tvm.mrt.symbol import *
tr = Trace.from_expr(expr, params, model_name="resnet18_v1")
# tr = tr.subgraph(onames=["%1"])
tr.checkpoint()
# tr.print(param_config={ "use_all": True, })

from tvm.mrt import fuse
from tvm.mrt import op
fuse_tr = tr.checkpoint_transform(
        fuse.FuseTupleGetItem.apply(),
        fuse.FuseBatchNorm.apply(),
        fuse.FuseAvgPool2D.apply(),
        tr_name = "fuse",
        # force=True,
        )
# fuse_tr.print(param_config={ "use_all": True, })

from tvm.mrt.calibrate import Calibrator, SymmetricMinMaxSampling

calib_tr = fuse_tr.checkpoint_transform(
        Calibrator.apply(random_config={
            "enabled": True,
            "absmax": 1.0, }),
        print_bf=True, print_af=True,
)
# calib_tr.print()
# print(type(calib_tr.symbol))

from tvm.mrt.rules import slm
from tvm.mrt.quantize import Quantizer

# calib_tr = calib_tr.subgraph(onames=["%5"])
dt_tr = calib_tr.checkpoint_transform(
        SymmetricMinMaxSampling.apply(),
        slm.SymmetricLinearDiscretor.apply(),
        )
# dt_tr.print(short=True)
dt_tr: Trace = dt_tr.checkpoint_transform(
        Quantizer.apply(),
        # print_bf=True, print_af=True,
        # force=True,
)

# TODO(wlt): add symbol extra attrs for name_hint to search
#   in subgraph.
# TODO: extra attrs copy and assign logic.
from tvm.mrt.fixed_point import FixPoint, Simulator
# dt_tr.print(short=True, prefix_layers=20)
# FuseBatchNorm.%1
sim_tr = dt_tr.checkpoint_transform(
        Simulator.apply(),
        # force=True,
        )
# sim_tr.log()
# sim_tr.print(short=True)

qt_tr = dt_tr.checkpoint_transform(
        FixPoint.apply(),
        # print_bf = True, print_af = True,
        # force=True,
)
# qt_tr.log()
qt_tr.print(short=True)

from tvm.mrt.sym_cvm import to_cvm
import cvm

# params
print("Transform TVM params into CVM")
cvm_params = {}
qt_pa = qt_tr.params
for key in qt_pa:
    if qt_pa[key].shape == ():
        np_data = [qt_pa[key].asnumpy().astype("int32")]
    else:
        np_data = qt_pa[key].asnumpy().astype("int32")
    cvm_params[key] = cvm.nd.array(np_data)
print("Transform TVM symbol into CVM finished")

# symbol
print("Transform TVM symbol into CVM")
qt_sy = qt_tr.symbol
qt_dict = to_cvm(qt_sy, qt_pa, cvm_params)
print("Transform TVM symbol into CVM finished")

print("CVM Json&Params dump")
with open("data/resnet18_v1/symbol", "w") as f:
    f.write(json.dumps(qt_dict["symbol"], indent=2, ensure_ascii=False))
param_bytes = cvm.nd.save_param_dict(qt_dict["params"])
with open("data/resnet18_v1/params", "wb") as f:
    f.write(param_bytes)

from tvm.mrt.zkml import circom, transformer

print(">>> Generating circom code ...")
symbol, params = qt_tr.symbol, qt_tr.params
symbol = transformer.shape_adapter(symbol)
out = transformer.model2circom(symbol, params)
code = circom.generate(out)
input_json = transformer.input_json(symbol, params)

print(">>> Generated, dump to {} ...".format(args.output))
#  print(code)
with open(args.output + ".circom", "w") as f:
    f.write(code)
with open(args.output + ".json", "w") as f:
    f.write(json.dumps(input_json, indent=2))


sys.exit(-1)

config = {
        "device": tvm.runtime.cuda(1),
        "target": tvm.target.cuda() }
def eval_single_image():
    global tr, sim_tr, qt_tr

    data_shape = (1, ) + image_shape
    print(data_shape, data_shape)
    tr = tr.set_input_shape(data_shape)
    sim_tr = sim_tr.set_input_shape(data_shape)
    qt_tr = qt_tr.set_input_shape(data_shape)
    data = get_real_image(*image_shape[1:])
    res = tr.eval(data, **config)
    print("tr: ", res.flatten()[:5])
    res = sim_tr.eval(data, **config)
    print("sim tr: ", res.flatten()[:5])
    res = qt_tr.eval(data, **config)
    print("qt tr: ", res.flatten()[:5])
    sys.exit(-1)
# eval_single_image()

from tvm.mrt.dataset_torch import TorchImageNet
ds = TorchImageNet(
        batch_size=batch_size,
        img_size=image_shape[1:],)
runtime.multiple_validate(
        tr.populate(**config),
        sim_tr.populate(**config),
        qt_tr.populate(**config),
        dataset=ds,
        stats_type=stats.ClassificationOutput,
        max_iter_num=20,
)
sys.exit(-1)


# qt_expr = qt_tr.to_expr()
# print(qt_expr)
# print(qt_expr.astext(show_meta_data=False))

# test accuracy

# torch_dataset = TorchImageNet()
# data, label = torch_dataset.next()
# print(data.shape, label.shape)
# sys.exit(-1)

# tr.print()
# outs = tr.calibrate()
# print(outs.keys())

# tr_eval = tr.eval(ctx)
# runtime.multiple_validate(tr_eval, TorchImageNet(),
#         stats.ClassificationOutput,)

# fuse pass: fold_constant, fuse_batch_norm, quantize

# compare accuracy

# to_cvm

# for k, v in params.items():
#     print(k, type(v))
#     continue
# set show_meta_data=True if you want to show meta data
# print(mod.astext(show_meta_data=False))

# @ir.transform.module_pass(opt_level=2)
# def transform(mod, ctx):
#     tp = relay.TensorType((10,), "float32")
#     x = relay.var("x", tp)
#     func = relay.Function([x], relay.abs(x))
#     gv = relay.GlobalVar("myabs")
#     # new_mod = tvm.IRModule({gv: func})
#     new_mod = tvm.IRModule()
#     new_mod["myabs"] = func
#     new_mod.update(mod)
#     return new_mod

# print(relay.analysis.all_vars(mod["main"]))

# module_pass = transform
# assert isinstance(module_pass, ir.transform.ModulePass)
# assert module_pass.info.opt_level == 2

x = relay.var("x", shape=(1, 3, 28, 28), dtype="float32")
y = relay.var("y", shape=(28,), dtype="float32")
out = x + y
out = relay.abs(out)
a = relay.Constant(tvm.nd.array(np.ones((28,), dtype="float32")))
b = relay.Constant(tvm.nd.array(np.ones((28,), dtype="float32")))
c = a + b
out = out + c
relay.analysis.post_order_visit(out, _collect_ops)

mod = tvm.IRModule()
mod["main"] = relay.Function([x, y], out)
mod = relay.transform.FoldConstant()(mod)

print(mod.astext(show_meta_data=False))
sys.exit(1)

# mod = tvm.IRModule()
# mod["main"] = relay.Function([x, y], out)
# print(str(mod))

# mod = module_pass(mod)
# print("2", str(mod))

# # out = mod["myabs"](out)
# # mod["main"] = relay.Function([x, y], out)
# # print("1", str(mod))

# # mod = create_relay_module_from_model() # Output: Figure 1
import pprint
from tvm.relay.op.contrib import register
from tvm.relay.op.contrib import cvm
pattern_table = register.get_pattern_table("cvm")
pprint.pprint([p[0] for p in pattern_table])
mod = relay.transform.MergeComposite(pattern_table)(mod)
#  mod = relay.transform.AnnotateTarget(["dnnl"])(mod) # Output: Figure 2
#  mod = relay.transform.MergeCompilerRegions()(mod) # Output: Figure 3
#  mod = relay.transform.PartitionGraph()(mod) # Output: Figure 4
print("3", mod.astext(show_meta_data=False))