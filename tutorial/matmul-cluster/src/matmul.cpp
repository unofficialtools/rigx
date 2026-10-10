// Reads "m k n", then A (m×k) and B (k×n) from stdin; prints C = A·B.
#include <eigen3/Eigen/Dense>
#include <iostream>

int main() {
    int m, k, n;
    if (!(std::cin >> m >> k >> n)) return 1;
    Eigen::MatrixXd A(m, k), B(k, n);
    for (int i = 0; i < m; ++i)
        for (int j = 0; j < k; ++j) std::cin >> A(i, j);
    for (int i = 0; i < k; ++i)
        for (int j = 0; j < n; ++j) std::cin >> B(i, j);

    Eigen::MatrixXd C = A * B;
    std::cout << m << " " << n << "\n"
              << C.format(Eigen::IOFormat(Eigen::FullPrecision)) << "\n";
}
