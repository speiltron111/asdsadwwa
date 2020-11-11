from itertools import combinations
import torch
from torch import nn


class MixITLossWrapper(nn.Module):
    r""" Mixture invariant loss wrapper.

    Args:
        loss_func: function with signature (targets, est_targets, **kwargs).
        pit_from (str): Determines how MixIT is applied.

            * ``'mix_it'``(mixture invariant): `loss_func` computes the
              loss for a given partition of the sources. Valid for any
              number of mixtures as soon as they contain the same number
              of sources.
              Output shape : :math:`(batch)`.
              See :meth:`~MixITLossWrapper.best_part_mix_it`.
            * ``'mix_it_gen'``(mixture invariant generalized): `loss_func`
              computes the loss for a given partition of the sources.
              Valid only for two mixtures, but those mixtures do not
              necessarly have to contain the same number of sources.
              Output shape : :math:`(batch)`.
              See :meth:`~MixITLossWrapper.best_part_mix_it_gen`.

    For each of these modes, the best partition and reordering will be
    automatically computed.

    Examples:
        >>> import torch
        >>> from asteroid.losses import singlesrc_mse
        >>> mixtures = torch.randn(10, 2, 16000)
        >>> est_sources = torch.randn(10, 4, 16000)
        >>> # Compute MixIT loss based on pairwise losses
        >>> loss_func = MixITLossWrapper(singlesrc_mse, pit_from='mix_it')
        >>> loss_val = loss_func(est_sources, mixtures)
    """
    def __init__(self, loss_func, pit_from='pw_mtx'):
        super().__init__()
        self.loss_func = loss_func
        self.pit_from = pit_from
        if self.pit_from not in ['mix_it', 'mix_it_gen']:
            raise ValueError('Unsupported loss function type for now. Expected'
                             'one of [`mix_it`, `mix_it_gen`]')

    def forward(self, est_targets, targets, return_est=False, **kwargs):
        """ Find the best partition and return the loss.

        Args:
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nmix, *].
                The batch of training targets
            return_est: Boolean. Whether to return the estimated mixtures
                estimates (To compute metrics or to save example).
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            - Best partition loss for each batch sample, average over
                the batch. torch.Tensor(loss_value)
            - The estimated mixtures (estimated sources summed according
                to the partition) if return_est is True.
                torch.Tensor of shape [batch, nmix, *].
        """

        # check input dimensions
        assert est_targets.shape[0] == targets.shape[0]
        assert est_targets.shape[2] == targets.shape[2]

        if self.pit_from == 'mix_it':
            min_loss, min_loss_idx, parts = self.best_part_mix_it(
                self.loss_func, est_targets, targets, **kwargs
            )

        elif self.pit_from == 'mix_it_gen':
            min_loss, min_loss_idx, parts = self.best_part_mix_it_generalized(
                self.loss_func, est_targets, targets, **kwargs
            )

        else:
            return

        # Take the mean over the batch
        mean_loss = torch.mean(min_loss)
        if not return_est:
            return mean_loss
       
        # order sources and sum them according to the best partition to obtain the estimated mixtures
        reordered = self.reorder_source(est_targets, targets, min_loss_idx, parts)
        return mean_loss, reordered


    @staticmethod
    def best_part_mix_it(loss_func, est_targets, targets, **kwargs):
        """ Find best partition of the estimated sources that gives
            the minimum loss for the MixIT training paradigm in [1].
             Valid for any number of mixtures as soon as they contain
             the same number of sources.

        Args:
            loss_func: function with signature (targets, est_targets, **kwargs)
                The loss function batch losses from.
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nmix, *].
                The batch of training targets.
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.LongTensor`: The indexes of the best partition.

                :class:`list`: list of the possible partitions of the sources.

        References:
            [1] Scott Wisdom and Efthymios Tzinis and Hakan Erdogan and Ron J Weiss
            and Kevin Wilson and John R Hershey, "Unsupervised sound separation using
            mixtures of mixtures." arXiv preprint arXiv:2006.12701 (2020) $
        """

        nmix = targets.shape[1]        # number of mixtures
        nsrc = est_targets.shape[1]    # number of estimated sources
        if nsrc % nmix != 0:
            raise ValueError('The mixtures are assumed to contain the same number of sources')
        nsrcmix = nsrc // nmix         # number of sources in each mixture

        # Generate all unique partitions of size k from a list lst of
        # length n, where l = n // k is the number of parts. The total
        # number of such partitions is: NPK(n,k) = n! / ((k!)^l * l!)
        # Algorithm recursively distributes items over parts
        def parts_mixit(lst, k, l):
            if l == 0:
                yield []
            else:
                for c in combinations(lst, k):
                    rest = [x for x in lst if x not in c]
                    for r in parts_mixit(rest, k, l-1):
                        yield [list(c), *r]

        # Generate all the possible partitions
        loss_set = []
        parts = list(parts_mixit(range(nsrc), nsrcmix, nmix))    
        for partition in parts:
            assert len(partition[0]) == nsrcmix
            assert len(partition) == nmix
       
            # sum the sources according to the given partition
            est_mixes = torch.stack([torch.sum(est_targets[:, indexes, :], axis=1) for indexes in partition], axis=1)

            # get loss for the given partition
            loss_set.append(loss_func(est_mixes, targets, **kwargs)[:, None])

        loss_set = torch.cat(loss_set, dim=1)

        # Indexes and values of min losses for each batch element
        min_loss, min_loss_indexes = torch.min(loss_set, dim=1, keepdim=True)
        assert len(min_loss_indexes) == est_mixes.shape[0]

        return min_loss, min_loss_indexes, parts

    @staticmethod
    def best_part_mix_it_generalized(loss_func, est_targets, targets, **kwargs):
        """ Find best partition of the estimated sources that gives
            the minimum loss for the MixIT training paradigm in [1].
            Valid only for two mixtures, but those mixtures do not
            necessarly have to contain the same number of sources.
            It is allowed the case where one mixture is silent.

        Args:
            loss_func: function with signature (targets, est_targets, **kwargs)
                The loss function batch losses from.
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nmix, *].
                The batch of training targets.
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.LongTensor`: The indexes of the best permutations.

                :class:`list`: list of the possible partitions of the sources.

        References:
            [1] Scott Wisdom and Efthymios Tzinis and Hakan Erdogan and Ron J Weiss
            and Kevin Wilson and John R Hershey, "Unsupervised sound separation using
            mixtures of mixtures." arXiv preprint arXiv:2006.12701 (2020) $
        """

        nmix = targets.shape[1]        # number of mixtures
        nsrc = est_targets.shape[1]    # number of estimated sources
        if nmix != 2:
            raise ValueError('Works only with two mixtures')

        # Generate all unique partitions of any size from a list lst of
        # length n. Algorithm recursively distributes items over parts
        def parts_mixit_gen(lst):
            partitions = []
            for k in range(len(lst) + 1):
                for c in combinations(lst, k):
                    rest = [x for x in lst if x not in c]
                    partitions.append([list(c), rest]) 
            return partitions

        # Generate all the possible partitions
        loss_set = []
        parts = parts_mixit_gen(range(nsrc))
        for partition in parts:
            assert len(partition) == nmix
        
            # sum the sources according to the given partition
            est_mixes = torch.stack([torch.sum(est_targets[:, indexes, :], axis=1) for indexes in partition], axis=1)

            # get loss for the given partition
            loss_set.append(loss_func(est_mixes, targets, **kwargs)[:, None])

        loss_set = torch.cat(loss_set, dim=1)

        # Indexes and values of min losses for each batch element
        min_loss, min_loss_indexes = torch.min(loss_set, dim=1, keepdim=True)
        assert len(min_loss_indexes) == est_mixes.shape[0]

        return min_loss, min_loss_indexes, parts

    @staticmethod
    def reorder_source(est_targets, targets, min_loss_idx, parts):
        """ Reorder sources according to the best partition.

        Args:
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nmix, *].
                The batch of training targets.
            min_loss_idx: torch.LongTensor. The indexes of the best permutations.
            parts: list of the possible partitions of the sources.

        Returns:
            :class:`torch.Tensor`:
                Reordered sources of shape [batch, nmix, time].

        """
        # For each batch there is a different min_loss_idx
        ordered = torch.zeros_like(targets)
        for b, idx in enumerate(min_loss_idx):
            right_partition = parts[idx]
            # sum the estimated sources to get the estimated mixtures
            ordered[b, :, :] = torch.stack([torch.sum(est_targets[b, indexes, :][None, :, :], axis=1) for indexes in right_partition], axis=1)

        return ordered
