import torch
import numpy as np

def kernel(x1, x2, noise=1, l=1):
  """
  x1 (torch.Tensor) : d × m tensor where m is the number of x vectors and d is the
                      dimensionality of the x vectors
  x2 (torch.Tensor) : d × n tensor n where is the number of x vectors and d is the
                      dimensionality of the x vectors
  """
  assert torch.is_tensor(x1) and torch.is_tensor(x2), 'x1 and x2 must be torch.Tensor'
  assert 0 < len(x1.shape) <= 2 and 0 < len(x2.shape) <= 2, 'x1 and x2 must be 1D or 2D'
  x1_mat = torch.atleast_2d(x1)
  x2_mat = torch.atleast_2d(x2)
  assert x1_mat.shape[0] == x2_mat.shape[0], 'vector dimensions of x1 and x2 are incompatible'

  x1_mat = torch.transpose(x1_mat.unsqueeze(2).expand(x1_mat.shape + (x2_mat.shape[1],)), 0, 1)  # [m, d, n]
  # the only thing different for other kernels is the last two lines
  scalar_form = torch.sum((x1_mat - x2_mat).pow(2), dim=1)  # [m, n]
  return noise* noise * torch.exp(-0.5 * scalar_form / (l * l))

  #norm = torch.sum(x1_mat.pow(2), dim=1) * torch.sum(x2_mat.pow(2), dim=0)
  #return noise * noise * torch.sum(x1_mat  * x2_mat , dim=1).pow(2) / norm

class SparseGaussianProcess():
    """
    TODO: device manipulation
    """
    def __init__(self, descriptor_dim):
        self.descriptor_dim = descriptor_dim
        self.full_descriptors = torch.empty((descriptor_dim, 0))
        self.sparse_descriptors = torch.empty((descriptor_dim, 0))
        self.training_outputs = torch.empty((0,))

        # hyperparameters
        self.kernel_noise = torch.tensor([1], dtype=torch.float64)
        self.kernel_length = torch.tensor([1], dtype=torch.float64)
        self.model_noise = torch.tensor([0.01], dtype=torch.float64)
        self.optimizer = torch.optim.Rprop([self.model_noise, self.kernel_noise, self.kernel_length])

        # trained model parameters (precomputed during model updates for faster prediction)
        self.Kss_inv = None
        self.Sigma = None
        self.alpha = None  # for predicting mean

    def update_model(self, x_train, y_train, x_sparse):
        """
        Updates GP model with a set of training vectors, i.e. update the Cholesky decomposed L

        Args:
          x_train (torch.Tensor): d × m tensor where m is the number of x vectors and d is the
                                  dimensionality of the x descriptor vectors
          y_train (torch.Tensor): m-dimensional vector of training outputs corresponding to
                                  the training inputs x_train
        """
        assert torch.atleast_2d(x_train).shape[0] == self.descriptor_dim,\
          'x_train\'s dimensions are incompatible with the descriptor dimensions of the model'
        self.sparse_descriptors = torch.cat((self.sparse_descriptors, torch.atleast_2d(x_sparse)), dim=1)
        self.full_descriptors = torch.cat((self.full_descriptors, torch.atleast_2d(x_train)), dim=1)
        self.training_outputs = torch.cat((self.training_outputs, y_train))
        self.__update_Sigma_alpha()

    def __update_Sigma_alpha(self):
        Kff = kernel(self.full_descriptors, self.full_descriptors,
                             self.kernel_noise, self.kernel_length)
        Ksf = kernel(self.sparse_descriptors, self.full_descriptors,
                             self.kernel_noise, self.kernel_length)
        Kss = kernel(self.sparse_descriptors, self.sparse_descriptors,
                             self.kernel_noise, self.kernel_length)
        
        #Kss = Kss #+ torch.eye(self.sparse_descriptors.shape[1]) * 1e-8
        Lss = torch.linalg.cholesky(Kss)
        self.Kss_inv = torch.cholesky_inverse(Lss)

        Lambda_inv = torch.eye(self.full_descriptors.shape[1]) / self.model_noise.pow(2)

        Sigma_inv = Kss + Ksf @ Lambda_inv @ Ksf.T

        #print(Ksf.T @ torch.tensor([-1.0, 1.0, 0.0], dtype=torch.float64))
        cond_Sigma_inv = torch.linalg.cond(Sigma_inv)
        print(cond_Sigma_inv.item())

        Lss_inv = torch.linalg.solve_triangular(Lss, torch.eye(Lss.shape[1]), upper=False)
        # V method
        V = Ksf.T @ Lss_inv.T
        C = torch.eye(V.shape[1]) + V.T @ Lambda_inv @ V
        cond_C = torch.linalg.cond(C)
        print(cond_C.item())

        # use Cholesky inverse (needs jitter!)
        Sigma_inv = Sigma_inv + torch.eye(self.sparse_descriptors.shape[1]) #* 1e-8
        #L_Sigma_inv = torch.linalg.cholesky(Sigma_inv)
        #self.Sigma = torch.cholesky_inverse(L_Sigma_inv)
        self.Sigma = torch.linalg.inv(Sigma_inv)
        #print(torch.max(torch.abs(torch.eye(self.sparse_descriptors.shape[1]) - self.Sigma @ Sigma_inv)))
        
        self.alpha = self.Sigma @ Ksf @ Lambda_inv @ self.training_outputs 

    def get_predictions(self, x_test, mean_var=[True, True], mode='sor'):
        """
        Get predictions of GP model with a set of testing vectors.

        Args:
          x_test (torch.Tensor): d × p tensor where p is the number of x vectors and d is the
                                  dimensionality of the x descriptor vectors
        """
        Kst = kernel(self.sparse_descriptors, x_test,
                     self.kernel_noise, self.kernel_length)
        predictions = []
        if mean_var[0]:
            mean = Kst.T @ self.alpha
            predictions.append(mean)
        if mean_var[1]:
            if mode == 'sor':  # quite nonsensical
                var = torch.abs((Kst.T @ self.Sigma @ Kst).diag())
            elif mode == 'dtc':
                var = kernel(x_test, x_test,
                             self.kernel_noise, self.kernel_length)
                var = var - Kst.T @ self.Kss_inv @ Kst
                var = var + Kst.T @ self.Sigma @ Kst
                var = torch.abs(var.diag())
            predictions.append(var)
        return predictions

    def get_likelihood(self):
        # TODO: make Ksf a class attribute
        Ksf = kernel(self.sparse_descriptors, self.full_descriptors,
                     self.kernel_noise, self.kernel_length)
        fsize = self.full_descriptors.shape[1]
        Lambda_inv = self.model_noise.pow(-1) * torch.eye(fsize)
        fit_term = -0.5 * self.training_outputs.unsqueeze(0) @ Lambda_inv
        fit_term = fit_term @ (self.training_outputs - Ksf.T @ self.alpha)
        Xi_inv_det =  torch.linalg.det(self.Kss_inv) / torch.linalg.det(self.Sigma) / torch.linalg.det(Lambda_inv)
        return fit_term - 0.5 * torch.log(Xi_inv_det) - fsize * 0.5 * np.log(2 * np.pi)

    def optimize_hyperparameters(self, rtol=1e-4, relax_kernel_length=False):
        """
        Optimize hyperparameters
        """
        self.model_noise.requires_grad_()
        self.kernel_noise.requires_grad_()
        self.kernel_length.requires_grad_(relax_kernel_length)
        counter = 0
        self.optimizer.zero_grad()
        self.__update_Sigma_alpha()
        likelihood = self.get_likelihood()
        (-likelihood).backward()
        dlikelihood = np.inf
        prev_likelihood = likelihood.item()
        while np.abs(dlikelihood/prev_likelihood) > rtol:
            counter += 1
            self.optimizer.step()
            self.optimizer.zero_grad()
            self.__update_Sigma_alpha()
            likelihood = self.get_likelihood()
            (-likelihood).backward()
            dlikelihood = np.abs(prev_likelihood - likelihood.item())
            prev_likelihood = likelihood.item()
        self.model_noise.requires_grad_(False)
        self.kernel_noise.requires_grad_(False)
        self.kernel_length.requires_grad_(False)

        self.Kss_inv = self.Kss_inv.detach()
        self.Sigma = self.Sigma.detach()
        self.alpha = self.alpha.detach()
        return counter