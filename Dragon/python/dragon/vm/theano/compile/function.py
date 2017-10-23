# --------------------------------------------------------
# Dragon
# Copyright(c) 2017 SeetaTech
# Written by Ting Pan
# --------------------------------------------------------

import copy
from collections import OrderedDict
import numpy as np
import sys

import dragon.core.mpi as mpi
import dragon.core.workspace as ws
import dragon.protos.dragon_pb2 as pb
from dragon.core.utils import MakeArgument
from dragon.core.gradient_maker import GraphGradientMaker
from dragon.core.scope import GetOperatorName, GetTensorName
from dragon.core.tensor import Tensor

def GraphDef_Grad(graph_def, targets):
    """Inject the gradient targets into GraphDef.

    Parameters
    ----------
    graph_def : dragon_pb2.GraphDef
        The definition of graph.
    targets : list
        The solving targets.

    Returns
    -------
    None

    See Also
    --------
    `T.grad(*args, **kwargs)`_ - How the generate gradient targets.

    """
    all_pairs = set()
    for target in targets:
        for wrt in target.grad_wrts:
            all_pairs.add((target.name, wrt))

    for pair in all_pairs:
        g_target = pb.GradientTarget()
        g_target.cost = str(pair[0])
        g_target.wrt = str(pair[1])
        graph_def.g_target.extend([g_target])


def GraphDef_Phase(graph_def, targets):
    """Inject the phase into GraphDef.

    If existing gradients, we assume it should be ``TRAIN``, and vice versa.

    Parameters
    ----------
    graph_def : dragon_pb2.GraphDef
        The definition of graph.
    targets : list
        The solving targets.

    Returns
    -------
    None

    """
    phase = 'TEST'
    from dragon.core.scope import PHASE_SCOPE
    global PHASE_SCOPE
    if PHASE_SCOPE != '': phase = PHASE_SCOPE.upper()
    else:
        for target in targets:
            if len(target.grad_wrts) > 0:
                phase = 'TRAIN'
                break
    graph_def.arg.extend([MakeArgument('phase', phase)])


def GraphDef_Update(graph_def, updater):
    """Inject the update targets into GraphDef.

    The ``updater`` should generate update targets before.

    Parameters
    ----------
    graph_def : dragon_pb2.GraphDef
        The definition of graph.
    updater : BaseUpdater
        The updater.

    Returns
    -------
    None

    """
    if updater is None: return

    updater._prefix = graph_def.name + '_'
    extra_arguments = updater._extra_kwargs
    extra_arguments['domain'] = updater._prefix
    parallel_arguments = {}

    # wrap hyper-parameters as Tensor for CC
    for k,v in updater._hyper_params.items():
        ws.FeedTensor(updater._prefix + k, np.array([v], dtype=np.float32))

    # check data parallel if necessary
    if mpi.Is_Init():
        idx, group = mpi.AllowParallel()
        if idx != -1:
            parallel_arguments['parallel_mode'] = mpi.GetParallelMode()
            parallel_arguments['comm'], parallel_arguments['group'] \
                = mpi.CreateGroup(root=group[0], incl=group)
            parallel_arguments['root'] = group[0]
        for k, v in parallel_arguments.items():
            graph_def.arg.add().CopyFrom(MakeArgument(k, v))

    for tuple in updater._tuples:
        tensors = tuple[0]; arguments = tuple[1]
        kwargs = dict(arguments, **extra_arguments)
        u_target = pb.UpdateTarget()
        u_target.type = updater._type
        _, u_target.name = GetOperatorName()
        for tensor in tensors:
            u_target.tensor.append(tensor)
        for k, v in kwargs.items():
            u_target.arg.add().CopyFrom(MakeArgument(k, v))
        graph_def.u_target.extend([u_target])


def GraphDef_Opt(graph_def):
    """Inject the optimization options into GraphDef.

    Parameters
    ----------
    graph_def : dragon_pb2.GraphDef
        The definition of graph.

    Returns
    -------
    None

    References
    ----------
    `config.SetDebugMode(*args, **kwargs)`_ - How the enable debug mode.

    `memonger.share_grads(*args, **kwargs)`_ - How the enable gradients sharing.

    """
    from dragon.config import option
    graph_def.debug_mode = option['debug_mode']
    graph_def.share_grads = option['share_grads']


def GraphDef_Device(graph_def):
    """Inject the device option into GraphDef.

    Parameters
    ----------
    graph_def : dragon_pb2.GraphDef
        The definition of graph.

    Returns
    -------
    None

    References
    ----------
    `config.EnableCPU()`_ - How to use CPU device.

    `config.EnableCUDA(*args, **kwargs)`_ - How to use CUDA device.

    `config.SetRandomSeed(*args, **kwargs)`_ - How to set random seed.

    """
    from dragon.config import option
    if option['device'] is not 'None':
        supports = {'CPU': 0, 'CUDA': 1}
        device_option = pb.DeviceOption()
        device_option.device_type = supports[option['device']]
        device_option.gpu_id = option['gpu_id']
        device_option.random_seed = option['random_seed']
        if option['use_cudnn']: device_option.engine = 'CUDNN'
        graph_def.device_option.CopyFrom(device_option)


def function(inputs=None, outputs=None, givens=None, updater=None):
    """Return a callable function that will compute ``outputs`` or apply ``updater``.

    Set ``inputs`` to feed inputs into this callable function.

    Set ``givens`` to substitute some tensors before making the computation graph.

    Set ``updater`` to make update graph, but the update targets should be generated before.

    Parameters
    ----------
    inputs : Tensor, list of Tensor or None
        The inputs to feed.
    outputs : Tensor, list of Tensor or None
        The outputs to solve.
    givens : dict or None
        The substitutions to use.
    updater : BaseUpdater
        The updater to use.

    Returns
    -------
    function
        The callable function.

    Examples
    --------
    >>> x = Tensor('x').Variable()
    >>> y = x * 2
    >>> f = theano.function(outputs=y)
    >>> x.set_value(np.ones((2, 3), dtype=np.float32))
    >>> print(f())
    >>> [[ 2.  2.  2.]
         [ 2.  2.  2.]]

    >>> f = theano.function(inputs=x, outputs=y)
    >>> print(f(np.ones((2, 3), dtype=np.float32)))
    >>> [[ 2.  2.  2.]
         [ 2.  2.  2.]]

    """
    if not isinstance(inputs, list):
        if inputs is None: inputs = []
        else: inputs = [inputs]
    if not isinstance(outputs, list):
        if outputs is None: outputs = []
        else: outputs = [outputs]

    if len(outputs) > 0 and updater is not None:
        raise RuntimeError('You can specific either outputs or updater, not both.')

    all_exprs = {}; all_extra_targets = set()
    if not isinstance(outputs, list): outputs = [outputs]

    graph_def = pb.GraphDef()

    graph_def.name = 'Graph_' + str(ws.CURRENT_GRAPH_IDX)
    ws.CURRENT_GRAPH_IDX += 1

    # extract operators and targets from expressions
    existing_grads = False
    for output in outputs:
        graph_def.target.extend([output.name])
        if sys.version_info >= (3, 0):
            all_exprs = OrderedDict(all_exprs, **output.expressions)
        else:
            all_exprs = dict(all_exprs, **output.expressions)
        all_extra_targets = all_extra_targets.union(output.extra_targets)
        if len(output.grad_wrts) > 0: existing_grads = True
    for extra_target in all_extra_targets: graph_def.target.extend([extra_target])

    # we should sort out the topology of these operators before using
    all_exprs = sorted(all_exprs.items(), key=lambda d:d[0])
    forward_ops = copy.deepcopy([v for k,v in all_exprs])

    # handle givens
    if givens is not None:
        name_dict = {}
        external_input_exprs = {}

        for old_tenosr, new_tensor in givens.items():
            if isinstance(new_tensor, Tensor):
                name_dict[old_tenosr.name] = new_tensor._name
                if sys.version_info >= (3, 0):
                    external_input_exprs = OrderedDict(external_input_exprs, **new_tensor.expressions)
                else:
                    external_input_exprs = dict(external_input_exprs, **new_tensor.expressions)
            elif isinstance(new_tensor, np.ndarray): ws.FeedTensor(new_tensor, GetTensorName())
        external_input_ops = [v for k,v in external_input_exprs.items()]
        for op in forward_ops:
            op.input.extend([name_dict[input] if input in name_dict
                                              else input for input in op.input])
            del op.input[:int(len(op.input)/2)]

        forward_ops = external_input_ops + forward_ops

    # handle grads
    if existing_grads:
        targets = [output.name for output in outputs]
        forward_ops, grad_ops = GraphGradientMaker.Make(forward_ops, targets)
    else: grad_ops = []
    graph_def.op.extend(forward_ops + grad_ops)

    if len(outputs) > 0:
        GraphDef_Device(graph_def)
        GraphDef_Opt(graph_def)
        GraphDef_Grad(graph_def, outputs)
        GraphDef_Phase(graph_def, outputs)

    elif updater is not None:
        GraphDef_Device(graph_def)
        GraphDef_Opt(graph_def)
        GraphDef_Update(graph_def, updater)

    # call c api to create graph
    ws.CreateGraph(graph_def)

    # return a lambda point to run this graph
    return lambda *args, **kwargs: \
        ws.RunGraph(graph_def.name, (inputs, args), outputs, **kwargs)