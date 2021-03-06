#
#  -*- coding: utf-8 -*-
#
#  Copyright (c) 2019 Intel Corporation
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import io
from contextlib import redirect_stdout, redirect_stderr
import tensorflow as tf
from tensorflow.core.framework import graph_pb2
from tensorflow.python.framework import importer
from tensorflow.python.framework import ops
from tensorflow.python.platform import gfile
from tensorflow.python.framework import graph_util
# from tensorflow.python.tools.optimize_for_inference_lib import optimize_for_inference

# from src.adaptor.tf_utils.quantize_graph import GraphRewriter
from .transform_graph.strip_unused import StripUnusedNodes
from .transform_graph.fold_batch_norm import FoldBatchNormNodes
from .transform_graph.insert_logging import InsertLogging
from .transform_graph.freeze_max_min import freeze_max
from .transform_graph.freeze_max_min import freeze_min
from .transform_graph.freeze_max_min import freeze_requantization_range
from .transform_graph.freeze_max_min import get_all_fp32_data, get_tensor_histogram, combine_histogram
from .transform_graph.fuse_quantized_conv_and_requantize import fuse_quantized_conv_and_requantize
from .transform_graph.fuse_column_wise_mul import FuseColumnWiseMul
from .transform_graph.rerange_quantized_concat import RerangeQuantizedConcat
from .util import write_graph
from .quantize_graph.quantize_graph_for_intel_cpu import QuantizeGraphForIntel
from .quantize_graph.quantize_graph_common import QuantizeGraphHelper
from .quantize_graph.quantize_graph_conv import FuseNodeStartWithConv2d
import os
import sys
import logging
import threading
import time
import numpy as np
import ast
import subprocess
import copy

TF_SUPPORTED_MAX_VERSION = '2.1.0'
TF_SUPPORTED_MIN_VERSION = '1.14.0'
from io import StringIO


class OutputGrabber(object):
    """
    Class used to grab standard output or another stream.
    """
    escape_char = "\b"

    def __init__(self, stream=None, threaded=False):
        self.origstream = stream
        self.threaded = threaded
        if self.origstream is None:
            self.origstream = sys.stdout
        self.origstreamfd = self.origstream.fileno()
        self.capturedtext = ""
        # Create a pipe so the stream can be captured:
        self.pipe_out, self.pipe_in = os.pipe()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, type, value, traceback):
        self.stop()

    def start(self):
        """
        Start capturing the stream data.
        """
        self.capturedtext = ""
        # Save a copy of the stream:
        self.streamfd = os.dup(self.origstreamfd)
        # Replace the original stream with our write pipe:
        os.dup2(self.pipe_in, self.origstreamfd)
        if self.threaded:
            # Start thread that will read the stream:
            self.workerThread = threading.Thread(target=self.readOutput)
            self.workerThread.start()
            # Make sure that the thread is running and os.read() has executed:
            time.sleep(0.01)

    def stop(self):
        """
        Stop capturing the stream data and save the text in `capturedtext`.
        """
        # Print the escape character to make the readOutput method stop:
        self.origstream.write(self.escape_char)
        # Flush the stream to make sure all our data goes in before
        # the escape character:
        self.origstream.flush()
        if self.threaded:
            # wait until the thread finishes so we are sure that
            # we have until the last character:
            self.workerThread.join()
        else:
            self.readOutput()
        # Close the pipe:
        os.close(self.pipe_in)
        os.close(self.pipe_out)
        # Restore the original stream:
        os.dup2(self.streamfd, self.origstreamfd)
        # Close the duplicate stream:
        os.close(self.streamfd)

    def readOutput(self):
        """
        Read the stream data (one byte at a time)
        and save the text in `capturedtext`.
        """
        while True:
            char = os.read(self.pipe_out,
                           10240).decode(self.origstream.encoding)
            if not char or self.escape_char == char[-1]:
                break
            self.capturedtext += char


class GraphConverter:
    def __init__(self,
                 input_graph,
                 output_graph,
                 inputs=[],
                 outputs=[],
                 qt_config={},
                 data_loader=None):
        """Convert graph.

        :param input_graph: input graph pb file.
        :param output_graph: output graph pb file. If set, output directory should be exist.
        :param inputs: input nodes' names.
        :param outputs: output nodes' names.
        :param excluded_ops: list of operations to be excluded from quantization.
        :param excluded_nodes: list of nodes to be excluded from quantization.
        :param per_channel: if set True, enables weight quantization channel-wise.
        """
        # For iLiT, the input_graph is not graph file path but Graph object.
        self.input_graph = input_graph.as_graph_def()
        self.output_graph = output_graph
        self.inputs = inputs
        self.outputs = outputs
        # quantize specific config
        # self.per_channel = per_channel
        # self.excluded_ops = excluded_ops
        # self.excluded_nodes = excluded_nodes
        self.algo = "Direct"
        self.calib_iteration = qt_config['calib_iteration']
        self.op_wise_config = qt_config['op_wise_config']
        self._low_precision_mode = 'eightbit'
        self._calibration_data = []
        self._fp32_print_data = []
        # self.gen_calib_data_cmds = self._inference
        self.debug = False
        self.data_loader = data_loader
        self._check_tf_version()
        self._check_args()
        self._gen_tmp_filenames()
        self._kl_op_dict = {}
        self._kl_keys = []
        self._print_node_mapping = {}
        self._enable_kl_op_names = [
            k for k in self.op_wise_config if self.op_wise_config[k][1] == 'kl'
        ]

    def load_graph(self, model_file):

        graph = tf.Graph()
        graph_def = tf.GraphDef()

        if not isinstance(model_file, graph_pb2.GraphDef):
            file_ext = os.path.splitext(model_file)[1]

            with open(model_file, "rb") as f:
                if file_ext == '.pbtxt':
                    text_format.Merge(f.read(), graph_def)
                else:
                    graph_def.ParseFromString(f.read())

            with graph.as_default():
                tf.import_graph_def(graph_def, name='')
        else:
            with graph.as_default():
                tf.import_graph_def(model_file, name='')

        return graph

    def _inference(self, input_graph):
        """Run the calibration on the input graph 

        Args:
            input_graph (tf.compat.v1.GraphDef): input graph
        """
        graph = self.load_graph(input_graph)
        input_tensor = graph.get_tensor_by_name(self.inputs[0] + ":0")
        output_tensor = [
            graph.get_tensor_by_name(x + ":0") for x in self.outputs
        ]
        import tensorflow as tf

        config = tf.ConfigProto()
        config.inter_op_parallelism_threads = 2
        config.intra_op_parallelism_threads = int(
            subprocess.check_output(
                'cat /proc/cpuinfo | grep "cpu cores"|uniq|cut -d ":" -f 2',
                shell=True))

        quantize_batch = 0
        sess_graph = tf.compat.v1.Session(graph=graph, config=config)
        print("Sampling data...")
        for content in self.data_loader:
            try:
                np_images = content[0]
                predictions = sess_graph.run(output_tensor,
                                             {input_tensor: np_images})

                # print("Processed %d batches."% (quantize_batch + 1))
                quantize_batch += 1
                if quantize_batch == self.calib_iteration:  # set the quantize iteration to 100
                    break
            except tf.errors.OutOfRangeError:
                print("Running out of images from dataset.")
                break

    def _check_tf_version(self):
        is_supported_version = False
        try:
            from tensorflow import python
            if (hasattr(python, "pywrap_tensorflow")
                    and hasattr(python.pywrap_tensorflow, "IsMklEnabled")):
                from tensorflow.python.pywrap_tensorflow import IsMklEnabled
            else:
                from tensorflow.python._pywrap_util_port import IsMklEnabled
            if IsMklEnabled() and (TF_SUPPORTED_MIN_VERSION <= tf.__version__
                                   <= TF_SUPPORTED_MAX_VERSION):
                is_supported_version = True
        except Exception as e:
            raise ValueError(e)
        finally:
            if not is_supported_version:
                raise ValueError(
                    str('Please install Intel® Optimizations for TensorFlow'
                        ' or MKL enabled source build TensorFlow'
                        ' with version >={} and <={}').format(
                            TF_SUPPORTED_MIN_VERSION,
                            TF_SUPPORTED_MAX_VERSION))

    def _check_args(self):
        if self.output_graph and not os.path.exists(
                os.path.dirname(self.output_graph)):
            raise ValueError('"output_graph" directory does not exist.')

        self._output_path = os.path.dirname(
            os.path.realpath(
                self.output_graph if self.output_graph else self.input_graph))

    def _gen_tmp_filenames(self):
        self._fp32_optimized_graph = os.path.join(self._output_path,
                                                  'fp32_optimized_graph.pb')
        self._int8_dynamic_range_graph = os.path.join(
            self._output_path, 'int8_dynamic_range_graph.pb')
        self._int8_logged_graph = os.path.join(self._output_path,
                                               'int8_logged_graph.pb')
        self._fp32_logged_graph = os.path.join(self._output_path,
                                               'fp32_logged_graph.pb')
        self._int8_frozen_range_graph = os.path.join(
            self._output_path, 'int8_frozen_range_graph.pb')
        if not self.output_graph:
            self.output_graph = os.path.join(self._output_path,
                                             'int8_final_fused_graph.pb')
        # to keep temp graphDef
        self._tmp_graph_def = None

    def inspect_tensor(self, op_list, op_iteration_list):
        try:
            self._optimize_frozen_fp32_graph()
        except Exception as e:
            logging.error('Failed to optimize fp32 graph due to: %s', str(e))
            raise ValueError(e) from e
        else:
            return self.dump_tensor(op_list, op_iteration_list)

    def convert(self):
        """Do convert, including:
            1) optimize fp32_frozen_graph,
            2) quantize graph,
            3) calibration,
            4) fuse RequantizeOp with fused quantized conv, and so on.

        :return:
        """
        try:
            self._optimize_frozen_fp32_graph()
        except Exception as e:
            logging.error('Failed to optimize fp32 graph due to: %s', str(e))
            raise ValueError(e) from e
        else:
            return self.quantize()

    def _get_fp32_print_node_names(self, specified_op_list):
        offset_map = {
            "QuantizedConv2DWithBiasSumAndRelu": 3,
            "QuantizedConv2DWithBiasAndRelu": 2,
            "QuantizedConv2DWithBias": 1,
        }
        target_conv_op = []
        sorted_graph = QuantizeGraphHelper().get_sorted_graph(
            self._fp32_origin_graph, self.outputs)

        node_name_mapping = {
            node.name: node
            for node in self._tmp_graph_def.node if node.op != "Const"
        }

        for node in self._tmp_graph_def.node:
            if node.op in offset_map:
                target_conv_op.append(node.name.split('_eightbit_')[0])
        fp32_node_name_mapping = {
            node.name: node
            for node in sorted_graph.node if node.op != "Const"
        }
        sorted_node_names = [
            i.name for i in sorted_graph.node if i.op != "Const"
        ]

        output_node_names = []
        for i in target_conv_op:
            if specified_op_list and i not in specified_op_list:
                continue
            if node_name_mapping[
                    i +
                    "_eightbit_quantized_conv"].op == 'QuantizedConv2DWithBiasSumAndRelu':
                start_index = sorted_node_names.index(i)
                for index, value in enumerate(sorted_node_names[start_index:]):
                    if fp32_node_name_mapping[value].op.startswith(
                            "Add") and fp32_node_name_mapping[
                                sorted_node_names[start_index + index +
                                                  1]].op == "Relu":
                        output_node_names.append(
                            sorted_node_names[start_index + index + 1])
                        self._print_node_mapping[sorted_node_names[start_index
                                                                   + index +
                                                                   1]] = i

            elif i in sorted_node_names:
                start_index = sorted_node_names.index(i)
                end_index = start_index + offset_map[node_name_mapping[
                    i + "_eightbit_quantized_conv"].op]
                output_node_names.append(sorted_node_names[end_index])
                self._print_node_mapping[sorted_node_names[end_index]] = i

        for i in output_node_names:
            self._kl_keys.append(';' + i + '__print__;__KL')

        InsertLogging(self._fp32_origin_graph,
                      node_name_list=output_node_names,
                      message="__KL:",
                      summarize=-1,
                      dump_fp32=True).do_transformation()
        write_graph(self._fp32_origin_graph, self._fp32_logged_graph)
        return self._fp32_origin_graph

    def _dequantize(self, data, scale_info):
        original_shape = data.shape
        size = data.size
        new_data = data.reshape(size, )
        max_value = 255 if scale_info[0].find("Relu") != -1 else 127
        return np.array([float(i / max_value)
                         for i in new_data]).reshape(original_shape)

    def dump_tensor(self, original_op_list, iteration_list):
        graph_node_name_mapping = {}
        q_node_name = []
        fp32_node_name = []
        fp32_node_name_mapping = {}
        q_node_scale = {}
        sorted_graph = QuantizeGraphHelper().get_sorted_graph(
            self._fp32_origin_graph, self.outputs)
        graph_q_node_name = []
        op_name_type_dict = {}
        quantized_node_name_postfix = '_eightbit_requantize'
        for node in sorted_graph.node:
            node_name = node.name
            if node.op.find("Quantized") != -1:
                node_name = node.name.split(quantized_node_name_postfix)[0]
                graph_q_node_name.append(node_name)
            graph_node_name_mapping[node_name] = node

        for op_info in original_op_list:
            op_name = op_info[0]
            op_type = op_info[1]

            if op_type not in ( "conv2d"):
                continue
            op_name_type_dict[op_name] = op_type

            if op_name in graph_q_node_name:
                q_node_name.append(op_name + quantized_node_name_postfix)
                q_node = graph_node_name_mapping[op_name]
                q_out_min = graph_node_name_mapping[
                    q_node.input[-2]].attr["value"].tensor.float_val[0]
                q_out_max = graph_node_name_mapping[
                    q_node.input[-1]].attr["value"].tensor.float_val[0]
                q_node_scale[op_name + quantized_node_name_postfix] = (q_node.op, q_out_min, q_out_max)
            else:
                fp32_node_name.append(op_name)
                if graph_node_name_mapping[op_name].op in (
                        "Conv2D", "DepthwiseConv2dNative"):
                    _, matched_nodes = FuseNodeStartWithConv2d(
                        sorted_graph,
                        self.outputs,
                        # self.op_wise_config[node.name][0],
                        False,
                        op_name).get_longest_fuse()
                    if matched_nodes:
                        fp32_node_name_mapping[matched_nodes[-1]] = op_name
                else:
                    fp32_node_name_mapping[op_name] = op_name

        InsertLogging(sorted_graph,
                      node_name_list=fp32_node_name_mapping.keys(),
                      message="__KL:",
                      summarize=-1,
                      dump_fp32=True).do_transformation()

        if q_node_name:
            sorted_graph = InsertLogging(sorted_graph,
                                         node_name_list=q_node_name,
                                         message="__KL:",
                                         summarize=-1).do_transformation()
        dump_tensor_data = []

        with OutputGrabber(sys.stderr, True) as out:
            self._inference(sorted_graph)

        sys.stdout = sys.__stdout__  # reset
        # sys.stderr = sys.__stderr__
        self._parse_output(out.capturedtext, dump_tensor_data)

        target_iteration = iteration_list[0] - 1 if iteration_list else 0
        start_index = target_iteration * len(original_op_list)
        end_index = (target_iteration + 1) * len(original_op_list)
        result = {}
        for i in dump_tensor_data[start_index:end_index]:
            key = i.split('__print__')[0][1:]
            data = i.split(':')[1].strip()
            data = data.replace(' ', "', '")
            quoto_index = None
            for index, value in enumerate(data):
                if value != '[':
                    quoto_index = index
                    break
            data = data[:quoto_index] + "'" + data[
                quoto_index:-quoto_index] + "'" + data[-quoto_index:]
            data = data.replace("]][[", "']],[['")
            data = data.replace("][", "'],['")

            if key in fp32_node_name_mapping:
                key = fp32_node_name_mapping[key]
                result[(key, op_name_type_dict[key])] = np.array(ast.literal_eval(data), dtype=np.float)
            else:
                result_key = key.split(quantized_node_name_postfix)[0]
                result[(result_key, op_name_type_dict[result_key])] = self._dequantize(
                    np.array(ast.literal_eval(data), dtype=np.int),
                    q_node_scale[key])

        return result

    def quantize(self):
        """Quantize graph only (without optimizing fp32 graph), including:
            1) quantize graph,
            2) calibration,
            3) fuse RequantizeOp with fused quantized conv, and so on.

        :return:
        """
        try:
            self._quantize_graph()
            if self._enable_kl_op_names:
                self._get_fp32_print_node_names(self._enable_kl_op_names)
                self._generate_calibration_data(self._fp32_logged_graph,
                                                self._fp32_print_data, True)
            self._insert_logging()

            self._generate_calibration_data(self._int8_logged_graph,
                                            self._calibration_data)
            self._freeze_requantization_ranges(self._kl_op_dict,
                                               self._print_node_mapping)
            self._fuse_requantize_with_fused_quantized_conv()
        except Exception as e:
            logging.error('Failed to quantize graph due to: %s', str(e))
            raise ValueError(e) from e
        finally:
            if not self.debug:
                self._post_clean()

            graph = tf.Graph()
            with graph.as_default():
                tf.import_graph_def(self._tmp_graph_def, name='')
            return graph

    def _optimize_frozen_fp32_graph(self):
        """Optimize fp32 frozen graph."""

        self._tmp_graph_def = self.input_graph
        dtypes = self._get_dtypes(self._tmp_graph_def)
        # self._tmp_graph_def = optimize_for_inference(self._tmp_graph_def, self.inputs, self.outputs, dtypes, False)
        self._tmp_graph_def = FuseColumnWiseMul(
            self._tmp_graph_def).do_transformation()
        self._tmp_graph_def = StripUnusedNodes(self._tmp_graph_def,
                                               self.inputs, self.outputs,
                                               dtypes).do_transform()
        self._tmp_graph_def = graph_util.remove_training_nodes(
            self._tmp_graph_def, self.outputs)
        self._tmp_graph_def = FoldBatchNormNodes(
            self._tmp_graph_def).do_transform()
        if self.debug:
            write_graph(self._tmp_graph_def, self._fp32_optimized_graph)
        self._fp32_origin_graph = self._tmp_graph_def

    def _quantize_graph(self):
        """quantize graph."""

        g = ops.Graph()
        with g.as_default():
            importer.import_graph_def(self._tmp_graph_def)

        intel_quantizer = QuantizeGraphForIntel(self._tmp_graph_def,
                                                self.outputs,
                                                self.op_wise_config)
        self._tmp_graph_def = intel_quantizer.do_transform()

        if self.debug:
            write_graph(self._tmp_graph_def, self._int8_dynamic_range_graph)

    def _insert_logging(self):
        int8_dynamic_range_graph_def = graph_pb2.GraphDef()
        int8_dynamic_range_graph_def.CopyFrom(self._tmp_graph_def)
        # TODO need to insert op-wise logging op.

        InsertLogging(
            self._tmp_graph_def,
            ops=["RequantizationRange", "RequantizationRangePerChannel"],
            message="__requant_min_max:").do_transformation()
        InsertLogging(self._tmp_graph_def, ops=["Min"],
                      message="__min:").do_transformation()
        InsertLogging(self._tmp_graph_def, ops=["Max"],
                      message="__max:").do_transformation()
        write_graph(self._tmp_graph_def, self._int8_logged_graph)

        self._tmp_graph_def.CopyFrom(int8_dynamic_range_graph_def)

    def _parse_output(self, input_data, output_data):
        assert input_data.count(';') % 2 == 0
        semicolon_index = [
            index for index, value in enumerate(input_data) if value == ';'
        ][::2]

        for index, value in enumerate(semicolon_index[:-1]):
            output_data.append(
                ''.join(input_data[value:semicolon_index[index + 1]]).strip() +
                '\n')

        output_data.append(''.join(input_data[semicolon_index[-1]:]).strip() +
                           '\n')

    def _generate_calibration_data(self,
                                   graph,
                                   output_data,
                                   enable_kl_algo=False):
        with OutputGrabber(sys.stderr, True) as out:
            self._inference(graph)

        sys.stdout = sys.__stdout__  # reset
        # sys.stderr = sys.__stderr__
        self._parse_output(out.capturedtext, output_data)
        for line in output_data:
            if enable_kl_algo and line.rsplit(':')[0] in self._kl_keys:
                fp32_data = get_all_fp32_data(line.rsplit(':')[-1])
                key = self._print_node_mapping[line[1:].split('__print')
                                               [0]] + '_eightbit_requant_range'
                if key not in self._kl_op_dict:
                    self._kl_op_dict[key] = get_tensor_histogram(fp32_data)
                else:
                    self._kl_op_dict[key] = combine_histogram(
                        self._kl_op_dict[key], fp32_data)

    def _freeze_requantization_ranges(self,
                                      additional_data=None,
                                      _print_node_mapping=None):
        self._tmp_graph_def = freeze_max(self._tmp_graph_def,
                                         self._calibration_data)
        self._tmp_graph_def = freeze_min(self._tmp_graph_def,
                                         self._calibration_data)
        self._tmp_graph_def = freeze_requantization_range(
            self._tmp_graph_def, self._calibration_data, additional_data,
            _print_node_mapping)
        if self.debug:
            write_graph(self._tmp_graph_def, self._int8_frozen_range_graph)

    def _fuse_requantize_with_fused_quantized_conv(self):
        self._tmp_graph_def = fuse_quantized_conv_and_requantize(
            self._tmp_graph_def)
        # strip_unused_nodes with optimize_for_inference
        dtypes = self._get_dtypes(self._tmp_graph_def)
        # self._tmp_graph_def = optimize_for_inference(self._tmp_graph_def, self.inputs, self.outputs, dtypes, False)
        self._tmp_graph_def = StripUnusedNodes(self._tmp_graph_def,
                                               self.inputs, self.outputs,
                                               dtypes).do_transform()
        self._tmp_graph_def = graph_util.remove_training_nodes(
            self._tmp_graph_def, self.outputs)
        self._tmp_graph_def = FoldBatchNormNodes(
            self._tmp_graph_def).do_transform()
        RerangeQuantizedConcat(self._tmp_graph_def).do_transformation()
        if self.debug:
            write_graph(self._tmp_graph_def, self.output_graph)
            logging.info('Converted graph file is saved to: %s',
                         self.output_graph)

    def _get_dtypes(self, in_graph_def):
        # TODO: keep dtypes list order as input list?
        dtypes = []
        for n in in_graph_def.node:
            if n.name in self.inputs:
                dtypes.append(n.attr["dtype"].type)

        return dtypes

    def _post_clean(self):
        """Delete the temporarily files generated during the quantization process.

        :return: None
        """
        if gfile.Exists(self._int8_logged_graph):
            os.remove(self._int8_logged_graph)
