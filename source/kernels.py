import torch


class Kernel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.kernel_hyperparameters = None

    def get_duplicate_ids(self, x1, x2, tol=1e-8):
        """
        Get indices of duplicate points based on a kernel function with output range [0,1]
        within given tolerance. The duplicate indices correspond to that of x2 (i.e. we
        keep x1 as is and remove the duplicate indices from x2).

        Args:
            x1, x2 (torch.Tensor): d × m tensors where d is the dimensionality and m is the number
                                   of descriptor vectors
            tol (float)          : how close the entries of K(x1, x2) are to 1
        """
        if x1.shape[1] > x2.shape[1]:
            x_larger, x_smaller = x1, x2
        else:
            x_larger, x_smaller = x2, x1
        triu_ids = torch.triu_indices(x_smaller.shape[1], x_larger.shape[1], offset=int(torch.equal(x1, x2)))
        K = self(x_smaller, x_larger)
        duplicate_triu_ids = torch.nonzero((1 - K[triu_ids[0], triu_ids[1]]) < tol, as_tuple=True)[0]
        if x1.shape[1] > x2.shape[1]:
            duplicate_ids = triu_ids[0][duplicate_triu_ids]
        else:
            duplicate_ids = triu_ids[1][duplicate_triu_ids]
        return duplicate_ids

    def remove_duplicates(self, x1, x2, tol=1e-8):
        """
        Returns x2 with duplicates (based on comparisons to x1) removed
        within some tolerance.
        """
        ids_to_remove = self.get_duplicate_ids(x1, x2, tol)
        mask = torch.ones(x2.shape[1], dtype=torch.bool)
        mask[ids_to_remove] = False
        all_ids = torch.arange(x2.shape[1])
        ids_to_keep = all_ids[mask]
        return x2[:, ids_to_keep]


class SquaredExponentialKernel(Kernel):
    """
    Squared exponential (i.e. Gaussian) kernel.
    """
    def __init__(self, lengthscale=1.0):
        super().__init__()
        self.lengthscale = torch.tensor([lengthscale], dtype=torch.float64)
        self.kernel_hyperparameters = [self.lengthscale]

    def forward(self, x1, x2, diag=False):
        """
        x1 (torch.Tensor): d × m tensor where m is the number of x vectors and d is the
                           dimensionality of the x vectors
        x2 (torch.Tensor): d × n tensor n where is the number of x vectors and d is the
                           dimensionality of the x vectors
        diag (bool)      : whether to only evaluate diagonal components
        """
        assert torch.is_tensor(x1) and torch.is_tensor(x2), 'x1 and x2 must be torch.Tensor'
        assert 0 < len(x1.shape) <= 2 and 0 < len(x2.shape) <= 2, 'x1 and x2 must be 1D or 2D'
        x1_mat = torch.atleast_2d(x1)
        x2_mat = torch.atleast_2d(x2)
        assert x1_mat.shape[0] == x2_mat.shape[0], 'vector dimensions of x1 and x2 are incompatible'

        if not diag:
            x1_mat = torch.transpose(x1_mat.unsqueeze(2).expand(x1_mat.shape + (x2_mat.shape[1],)), 0, 1)  # [m, d, n]
        else:
            assert x1_mat.shape[1] == x2_mat.shape[1], 'diag = True requires same dims'
        # the only thing different for other kernels is the last two lines
        scalar_form = torch.sum((x1_mat - x2_mat).pow(2), dim=1)  # [m, n]
        return torch.exp(-0.5 * scalar_form / self.lengthscale.pow(2))
