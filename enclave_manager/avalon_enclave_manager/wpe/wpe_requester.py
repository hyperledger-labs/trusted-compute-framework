#!/usr/bin/env python3

# Copyright 2020 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import json
import random
import secrets
import logging
import argparse

import config.config as pconfig
import utility.logger as plogger
import avalon_crypto_utils.signature as signature
import avalon_crypto_utils.crypto_utility as crypto_utils
from error_code.error_status import SignatureStatus
from http_client.http_jrpc_client import HttpJrpcClient
from avalon_sdk.work_order.work_order_params import WorkOrderParams

logger = logging.getLogger(__name__)


class WPERequester():
    """
    JRPC requester acting on behalf of WorkOrderProcessorEnclaveManager
    """

    def __init__(self, config):
        """
        Constructor for WPERequester. Initialize the HTTP jrpc client.
        Parameters :
            @param config - dict of config read
        """
        self._uri_client = HttpJrpcClient(
            config["KMEListener"]["kme_listener_url"])

    def get_unique_verification_key(self, verification_key_nonce,
                                    encryption_key, verifying_key):
        """
        Request wrapper to get a unique id from the KME

        Parameters :
            @param verification_key_nonce - Random nonce generated by this WPE
            @param encryption_key - Encryption key of worker to encrypt request
            @param verifying_key - Verifying key of the worker
        Returns :
            @returns result - Result received from the KME which includes the
                              public verification key which is supposed to be
                              included in REPORTDATA by the WPE. None, in case
                              of failure.
        """

        workload_id = "kme-uid"
        in_data = json.dumps({"nonce": verification_key_nonce})

        # Create session key and iv to sign work order request
        session_key = crypto_utils.generate_key()
        session_iv = crypto_utils.generate_iv()

        wo_req = self._construct_wo_req(
            in_data, workload_id, encryption_key, session_key, session_iv)

        json_rpc_request = self._get_request_json("GetUniqueVerificationKey")
        json_rpc_request["params"] = {"wo_request": wo_req}
        response = self._post_and_get_result(json_rpc_request)

        if "result" in response:
            wo_response_json = response["result"]
            if self._verify_res_signature(wo_response_json, verifying_key):
                decrypted_res = crypto_utils.decrypted_response(
                    wo_response_json, session_key, session_iv)
                # Response contains an array of results. In this case, the
                # array has single element and the data field is of interest.
                # The data contains result,verification_key and
                # verification_key_signature delimited by ' '.
                return decrypted_res[0]['data']
            return None
        else:
            logger.error("Could not get a unique id from the KME : {}"
                         .format(response))
            return None

    def register_wo_processor(self, attestation_report, encryption_key,
                              verifying_key):
        """
        Request to register this WPE with the KME

        Parameters :
            @param attestation_report - The IAS attestation report/DCAP quote
            @param encryption_key - Encryption key of worker to encrypt request
            @param verifying_key - Verifying key of the worker
        Returns :
            @returns status - The status of the registration.
                              True, for success. None, in case of errors.
        """

        workload_id = "kme-reg"
        in_data = json.dumps({"attestation_report": attestation_report})

        # Create session key and iv to sign work order request
        session_key = crypto_utils.generate_key()
        session_iv = crypto_utils.generate_iv()

        wo_request = self._construct_wo_req(
            in_data, workload_id, encryption_key, session_key, session_iv)

        json_rpc_request = self._get_request_json("RegisterWorkOrderProcessor")
        json_rpc_request["params"] = {"wo_request": wo_request}
        response = self._post_and_get_result(json_rpc_request)

        if "result" in response:
            wo_response_json = response["result"]
            if self._verify_res_signature(wo_response_json, verifying_key):
                decrypted_res = crypto_utils.decrypted_response(
                    wo_response_json, session_key, session_iv)
                return decrypted_res
            return None
        else:
            logger.error("Could not register this WPE with the KME : {}"
                         .format(response))
            return None

    def preprocess_work_order(self, wo_request, encryption_key,
                              verifying_key):
        """
        Request to preprocess a work order

        Parameters :
            @param wo_request - The original work order request
            @param encryption_key - WPE's public encryption key
            @param verifying_key - Verifying key of the worker
        Returns :
            @returns result - Result from KME that includes the workorder
                              key info. None, in case of failure.
        """
        # @TODO : Need to construct a full work order request as with
        # uid and reg requests. Generating the session key here and
        # verifying after response is received.
        json_rpc_request = self._get_request_json("PreProcessWorkOrder")
        json_rpc_request["params"] = {"wo_request": wo_request,
                                      "encryption_key": encryption_key}
        response = self._post_and_get_result(json_rpc_request)

        # @TODO : Response handling to be implemented
        return None

    def _construct_wo_req(self, in_data, workload_id, encryption_key,
                          session_key, session_iv):
        """
        Construct the parameters for a standard work order request

        Parameters :
            @param in_data - In data to be passed to workload processor
            @param workload_id - Id of the target workload
            @encryption_key - Worker encryption key
            @session_key - Session key to be embedded in request
            @session_iv - Session key iv for encryption algorithm
        Returns :
            @returns A json request prepared using parameters passed
        """
        # Create work order
        # Convert workloadId to hex
        workload_id_hex = workload_id.encode("UTF-8").hex()
        work_order_id = secrets.token_hex(32)
        requester_id = secrets.token_hex(32)
        requester_nonce = secrets.token_hex(16)
        # worker id is not known here. Hence passing a random string
        worker_id = secrets.token_hex(32)
        # Create work order params
        wo_params = WorkOrderParams(
            work_order_id, worker_id, workload_id_hex, requester_id,
            session_key, session_iv, requester_nonce,
            result_uri=" ", notify_uri=" ",
            worker_encryption_key=encryption_key,
            data_encryption_algorithm="AES-GCM-256"
        )
        wo_params.add_in_data(in_data)

        # Encrypt work order request hash
        wo_params.add_encrypted_request_hash()

        return {
            "jsonrpc": "2.0",
            "method": workload_id,
            "id": random.randint(0, 100000),
            "params": json.loads(wo_params.to_string())
        }

    def _get_request_json(self, method):
        """
        Helper method to synthesize jrpc request JSON

        Parameters :
            @param method - JRPC method to be set in the method field
        Returns :
            @returns A dict representing the basic request JSON
        """
        return {
            "jsonrpc": "2.0",
            "method": method,
            "id": random.randint(0, 100000)
        }

    def _verify_res_signature(self, work_order_res, worker_verification_key):
        """
        Verify work order result signature

        Parameters :
            @param work_order_res - Result from work order response
            @param worker_verification_key - Worker verification key
        Returns :
            @returns True - If verification succeeds
                    False - If verification fails
        """
        sig_obj = signature.ClientSignature()
        status = sig_obj.verify_signature(
            work_order_res, worker_verification_key)
        if status == SignatureStatus.PASSED:
            logger.info("Signature verification successful")
        else:
            logger.error("Signature verification failed")
            return False
        return True

    def _post_and_get_result(self, json_rpc_request):
        """
        Helper method to serialize and send JRPC request and get response
        from the KME.

        Parameters :
            @param json_rpc_request - JSON containing RPC request
        Returns :
            @returns response - Response received from the KME
        """
        json_request_str = json.dumps(json_rpc_request)
        logger.info("Request to KME listener %s", json_request_str)
        response = self._uri_client._postmsg(json_request_str)
        logger.info("Response from KME %s", response)

        return response
