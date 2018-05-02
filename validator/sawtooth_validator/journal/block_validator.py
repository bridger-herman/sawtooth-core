# Copyright 2017 Intel Corporation
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
# ------------------------------------------------------------------------------

import logging

from sawtooth_validator.concurrent.threadpool import \
    InstrumentedThreadPoolExecutor
from sawtooth_validator.concurrent.atomic import ConcurrentSet
from sawtooth_validator.concurrent.atomic import ConcurrentMultiMap

from sawtooth_validator.journal.block_wrapper import BlockStatus
from sawtooth_validator.journal.block_wrapper import BlockWrapper
from sawtooth_validator.journal.block_wrapper import NULL_BLOCK_IDENTIFIER
from sawtooth_validator.journal.consensus.consensus_factory import \
    ConsensusFactory
from sawtooth_validator.journal.chain_commit_state import ChainCommitState
from sawtooth_validator.journal.chain_commit_state import DuplicateTransaction
from sawtooth_validator.journal.chain_commit_state import DuplicateBatch
from sawtooth_validator.journal.chain_commit_state import MissingDependency
from sawtooth_validator.journal.validation_rule_enforcer import \
    enforce_validation_rules
from sawtooth_validator.state.settings_view import SettingsViewFactory
from sawtooth_validator import metrics

from sawtooth_validator.state.merkle import INIT_ROOT_KEY


LOGGER = logging.getLogger(__name__)
COLLECTOR = metrics.get_collector(__name__)


class BlockValidationFailure(Exception):
    """
    Indication that a failure has occurred during block validation.
    """


class BlockValidationError(Exception):
    """
    Indication that an error occured during block validation and the validity
    of the block could not be determined.
    """


# Need to disable this new pylint check until the function can be refactored
# to return instead of raise StopIteration, which it does by calling next()
# pylint: disable=stop-iteration-return
def look_ahead(iterable):
    """Pass through all values from the given iterable, augmented by the
    information if there are more values to come after the current one
    (True), or if it is the last value (False).
    """
    # Get an iterator and pull the first value.
    it = iter(iterable)
    last = next(it)
    # Run the iterator to exhaustion (starting from the second value).
    for val in it:
        # Report the *previous* value (more to come).
        yield last, True
        last = val
    # Report the last value.
    yield last, False


class BlockValidator(object):
    """
    Responsible for validating a block, handles both chain extensions and fork
    will determine if the new block should be the head of the chain and return
    the information necessary to do the switch if necessary.
    """

    def __init__(self,
                 block_cache,
                 state_view_factory,
                 transaction_executor,
                 squash_handler,
                 identity_signer,
                 data_dir,
                 config_dir,
                 permission_verifier,
                 thread_pool=None):
        """Initialize the BlockValidator
        Args:
            block_cache: The cache of all recent blocks and the processing
                state associated with them.
            state_view_factory: A factory that can be used to create read-
                only views of state for a particular merkle root, in
                particular the state as it existed when a particular block
                was the chain head.
            transaction_executor: The transaction executor used to
                process transactions.
            squash_handler: A parameter passed when creating transaction
                schedulers.
            identity_signer: A cryptographic signer for signing blocks.
            data_dir: Path to location where persistent data for the
                consensus module can be stored.
            config_dir: Path to location where config data for the
                consensus module can be found.
            permission_verifier: The delegate for handling permission
                validation on blocks.
            thread_pool: (Optional) Executor pool used to submit block
                validation jobs. If not specified, a default will be created.
        Returns:
            None
        """
        self._block_cache = block_cache
        self._state_view_factory = state_view_factory
        self._transaction_executor = transaction_executor
        self._squash_handler = squash_handler
        self._identity_signer = identity_signer
        self._data_dir = data_dir
        self._config_dir = config_dir
        self._permission_verifier = permission_verifier

        self._settings_view_factory = SettingsViewFactory(state_view_factory)

        self._thread_pool = InstrumentedThreadPoolExecutor(1) \
            if thread_pool is None else thread_pool

        # Blocks that are currently being processed
        self._blocks_processing = ConcurrentSet()

        self._blocks_processing_gauge = COLLECTOR.gauge(
            'blocks_processing', instance=self)
        self._blocks_processing_gauge.set_value(0)

        # Descendant blocks that are waiting for an in process block
        # to complete
        self._blocks_pending = ConcurrentSet()
        self._blocks_pending_descendants = ConcurrentMultiMap()

        self._blocks_pending_gauge = COLLECTOR.gauge(
            'blocks_pending', instance=self)
        self._blocks_pending_gauge.set_value(0)

    def stop(self):
        self._thread_pool.shutdown(wait=True)

    def _get_previous_block_state_root(self, blkw):
        if blkw.previous_block_id == NULL_BLOCK_IDENTIFIER:
            return INIT_ROOT_KEY

        return self._block_cache[blkw.previous_block_id].state_root_hash

    def _validate_batches_in_block(self, blkw, prev_state_root):
        """
        Validate all batches in the block. This includes:
            - Validating all transaction dependencies are met
            - Validating there are no duplicate batches or transactions
            - Validating execution of all batches in the block produces the
              correct state root hash

        Args:
            blkw: the block of batches to validate
            prev_state_root: the state root to execute transactions on top of

        Raises:
            BlockValidationFailure:
                If validation fails, raises this error with the reason.
            MissingDependency:
                Validation failed because of a missing dependency.
            DuplicateTransaction:
                Validation failed because of a duplicate transaction.
            DuplicateBatch:
                Validation failed because of a duplicate batch.
        """
        if not blkw.block.batches:
            return

        scheduler = None
        try:
            chain_commit_state = ChainCommitState(
                blkw.previous_block_id,
                self._block_cache,
                self._block_cache.block_store)

            scheduler = self._transaction_executor.create_scheduler(
                self._squash_handler, prev_state_root)
            self._transaction_executor.execute(scheduler)

            chain_commit_state.check_for_duplicate_batches(
                blkw.block.batches)

            transactions = []
            for batch in blkw.block.batches:
                transactions.extend(batch.transactions)

            chain_commit_state.check_for_duplicate_transactions(
                transactions)

            chain_commit_state.check_for_transaction_dependencies(
                transactions)

            for batch, has_more in look_ahead(blkw.block.batches):
                if has_more:
                    scheduler.add_batch(batch)
                else:
                    scheduler.add_batch(batch, blkw.state_root_hash)

        except (DuplicateBatch,
                DuplicateTransaction,
                MissingDependency) as err:
            if scheduler is not None:
                scheduler.cancel()
            raise BlockValidationFailure(
                "Block {} failed validation: {}".format(blkw, err))

        except Exception:
            if scheduler is not None:
                scheduler.cancel()
            raise

        scheduler.finalize()
        scheduler.complete(block=True)
        state_hash = None

        for batch in blkw.batches:
            batch_result = scheduler.get_batch_execution_result(
                batch.header_signature)
            if batch_result is not None and batch_result.is_valid:
                txn_results = \
                    scheduler.get_transaction_execution_results(
                        batch.header_signature)
                blkw.execution_results.extend(txn_results)
                state_hash = batch_result.state_hash
                blkw.num_transactions += len(batch.transactions)
            else:
                raise BlockValidationFailure(
                    "Block {} failed validation: Invalid batch "
                    "{}".format(blkw, batch))

        if blkw.state_root_hash != state_hash:
            raise BlockValidationFailure(
                "Block {} failed state root hash validation. Expected {}"
                " but got {}".format(
                    blkw, blkw.state_root_hash, state_hash))

    def _validate_permissions(self, blkw, prev_state_root):
        """
        Validate that all of the batch signers and transaction signer for the
        batches in the block are permitted by the transactor permissioning
        roles stored in state as of the previous block. If a transactor is
        found to not be permitted, the block is invalid.
        """
        if blkw.block_num != 0:
            for batch in blkw.batches:
                if not self._permission_verifier.is_batch_signer_authorized(
                        batch, prev_state_root, from_state=True):
                    return False
        return True

    def _validate_on_chain_rules(self, blkw, prev_state_root):
        """
        Validate that the block conforms to all validation rules stored in
        state. If the block breaks any of the stored rules, the block is
        invalid.
        """
        if blkw.block_num != 0:
            return enforce_validation_rules(
                self._settings_view_factory.create_settings_view(
                    prev_state_root),
                blkw.header.signer_public_key,
                blkw.batches)
        return True

    def validate_block(self, blkw):
        if blkw.status == BlockStatus.Valid:
            return
        elif blkw.status == BlockStatus.Invalid:
            raise BlockValidationFailure(
                'Block {} is already invalid'.format(blkw))

        # pylint: disable=broad-except
        try:
            try:
                prev_block = self._block_cache[blkw.previous_block_id]
            except KeyError:
                prev_block = None
            else:
                if prev_block.status == BlockStatus.Invalid:
                    raise BlockValidationFailure(
                        "Block {} rejected due to invalid predecessor"
                        " {}".format(blkw, prev_block))
                elif prev_block.status == BlockStatus.Unknown:
                    raise BlockValidationError(
                        "Attempted to validate block {} before its predecessor"
                        " {}".format(blkw, prev_block))

            try:
                prev_state_root = self._get_previous_block_state_root(blkw)
            except KeyError:
                raise BlockValidationError(
                    'Block {} rejected due to missing predecessor'.format(
                        blkw))

            if not self._validate_permissions(blkw, prev_state_root):
                raise BlockValidationFailure(
                    'Block {} failed permission validation'.format(blkw))

            consensus = self._load_consensus(prev_block)
            public_key = \
                self._identity_signer.get_public_key().as_hex()
            consensus_block_verifier = consensus.BlockVerifier(
                block_cache=self._block_cache,
                state_view_factory=self._state_view_factory,
                data_dir=self._data_dir,
                config_dir=self._config_dir,
                validator_id=public_key)

            if not consensus_block_verifier.verify_block(blkw):
                raise BlockValidationFailure(
                    'Block {} failed {} consensus validation'.format(
                        blkw, consensus))

            if not self._validate_on_chain_rules(blkw, prev_state_root):
                raise BlockValidationFailure(
                    'Block {} failed on-chain validation rules'.format(
                        blkw))

            self._validate_batches_in_block(blkw, prev_state_root)

            blkw.status = BlockStatus.Valid

        except BlockValidationFailure as err:
            blkw.status = BlockStatus.Invalid
            raise err

        except BlockValidationError as err:
            blkw.status = BlockStatus.Unknown
            raise err

        except Exception as e:
            LOGGER.exception(
                "Unhandled exception BlockValidator.validate_block()")
            raise e

    def _load_consensus(self, block):
        """Load the consensus module using the state as of the given block."""
        if block is not None:
            return ConsensusFactory.get_configured_consensus_module(
                block.header_signature,
                BlockWrapper.state_view_for_block(
                    block,
                    self._state_view_factory))
        return ConsensusFactory.get_consensus_module('genesis')

    def submit_blocks_for_verification(self, blocks, callback):
        for block in blocks:
            if self.in_process(block.header_signature):
                LOGGER.debug("Block already in process: %s", block)
                continue

            if self.in_process(block.previous_block_id):
                LOGGER.debug(
                    "Previous block '%s' in process,"
                    " adding '%s' pending",
                    block.previous_block_id, block)
                self._add_block_to_pending(block)
                continue

            if self.in_pending(block.previous_block_id):
                LOGGER.debug(
                    "Previous block '%s' is pending,"
                    " adding '%s' pending",
                    block.previous_block_id, block)
                self._add_block_to_pending(block)
                continue

            try:
                prev_block = self._block_cache[block.previous_block_id]
            except KeyError:
                LOGGER.error(
                    "Block %s submitted for processing but predecessor %s is"
                    " missing. Adding to pending.",
                    block,
                    block.previous_block_id)
                self._add_block_to_pending(block)
                continue
            else:
                if prev_block.status == BlockStatus.Unknown:
                    LOGGER.warning(
                        "Block %s submitted for processing but predecessor %s"
                        " has not been validated and is not pending. Adding to"
                        " pending.", block, prev_block)
                    self._add_block_to_pending(block)

            LOGGER.debug(
                "Adding block %s for processing", block.identifier)

            # Add the block to the set of blocks being processed
            self._blocks_processing.add(block.identifier)

            self._update_gauges()

            # Schedule the block for processing
            self._thread_pool.submit(
                self.process_block_verification, block, callback)

    def _update_gauges(self):
        self._blocks_pending_gauge.set_value(len(self._blocks_pending))
        self._blocks_processing_gauge.set_value(len(self._blocks_processing))

    def _release_pending(self, block):
        """Removes the block from processing and returns any blocks that should
        now be scheduled for processing, cleaning up the pending block trackers
        in the process.
        """
        LOGGER.debug("Removing block from processing %s", block.identifier)
        try:
            self._blocks_processing.remove(block.identifier)
        except KeyError:
            LOGGER.warning(
                "Tried to remove block from in process but it wasn't in"
                " processes: %s",
                block.identifier)

        if block.status == BlockStatus.Valid:
            # Submit all pending blocks for validation
            blocks_now_ready = self._blocks_pending_descendants.pop(
                block.identifier, [])
            for blk in blocks_now_ready:
                self._blocks_pending.remove(blk.identifier)
            return blocks_now_ready

        if block.status == BlockStatus.Invalid:
            # Mark all pending blocks as invalid
            blocks_now_invalid = self._blocks_pending_descendants.pop(
                block.identifier, [])

            while blocks_now_invalid:
                invalid_block = blocks_now_invalid.pop()
                invalid_block.status = BlockStatus.Invalid
                self._blocks_pending.remove(invalid_block.identifier)

                LOGGER.debug(
                    'Marking descendant block invalid: %s',
                    invalid_block)

                # Get descendants of the descendant
                blocks_now_invalid.extend(
                    self._blocks_pending_descendants.pop(
                        invalid_block.identifier, []))
            return []

        # An error occured during validation, something is wrong internally
        # and we need to abort validation of this block and all its
        # children without marking them as invalid.
        blocks_to_remove = self._blocks_pending_descendants.pop(
            block.identifier, [])

        while blocks_to_remove:
            block = blocks_to_remove.pop()
            self._blocks_pending.remove(block.identifier)

            LOGGER.debug(
                'Removing block from cache and pending due to error '
                'during validation: %s', block)

            del self._block_cache[block.identifier]

            # Get descendants of the descendant
            blocks_to_remove.extend(
                self._blocks_pending_descendants.pop(block.identifier, []))
        return []

    def in_process(self, block_id):
        return block_id in self._blocks_processing

    def in_pending(self, block_id):
        return block_id in self._blocks_pending

    def _add_block_to_pending(self, block):
        self._blocks_pending.add(block.identifier)
        previous = block.previous_block_id
        self._blocks_pending_descendants.append_if_unique(previous, block)

    def process_block_verification(self, block, callback):
        """
        Main entry for Block Validation, Take a given candidate block
        and decide if it is valid then if it is valid determine if it should
        be the new head block. Returns the results to the ChainController
        so that the change over can be made if necessary.
        """
        while True:
            chain_head = self._block_cache.block_store.chain_head
            try:
                self.validate_block(block)
                LOGGER.info(
                    'Block %s passed validation', block)
            except BlockValidationFailure as err:
                LOGGER.warning(
                    'Block %s failed validation: %s', block, err)
            except BlockValidationError as err:
                LOGGER.error(
                    'Encountered an error while validating %s: %s', block, err)
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception(
                    "Block validation failed with unexpected error: %s", block)

            # The validity of blocks depends partially on whether or not there
            # are any duplicate transactions or batches in the block. This can
            # only be checked accurately if the block store does not update
            # during validation. The current practice is the assume this will
            # not happen and, if it does, to reprocess the validation. This
            # has been experimentally proven to be more performant than locking
            # the chain head and block store around duplicate checking.
            if chain_head is None:
                break
            else:
                current_chain_head = self._block_cache.block_store.chain_head
                if chain_head.identifier == current_chain_head.identifier:
                    break
                else:
                    LOGGER.warning(
                        "Chain head updated from %s to %s while validating"
                        " block %s. Reprocessing validation.",
                        chain_head, current_chain_head, block)
                    block.status = BlockStatus.Unknown

        try:
            blocks_now_ready = self._release_pending(block)
            self.submit_blocks_for_verification(blocks_now_ready, callback)
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception(
                "Submitting pending blocks failed with unexpected error: %s",
                block)

        callback(block)
