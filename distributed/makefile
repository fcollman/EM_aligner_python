include ${PETSC_DIR}/install/lib/petsc/conf/variables
include ${PETSC_DIR}/install/lib/petsc/conf/rules

#"The Modular Component Architecture (MCA) is the backbone for much of Open MPI's functionality. It is a series of frameworks, components, and modules that are assembled at run-time to create an MPI implementation."
#https://www.open-mpi.org/faq/?category=tuning#mca-def
mca_opts=--mca btl_tcp_if_include 10.128.124.0/99 --mca plm_rsh_no_tree_spawn 1
#mpi_opts=-N 1 -hostfile ./hosts -prefix ${PETSC_DIR}/install ${mca_opts}
mpi_opts=-N 1 -hostfile ./hosts ${mca_opts}
mpi_opts_one=-N 1 ${mca_opts}
pastix_opts=-ksp_type preonly -pc_type lu -pc_factor_mat_solver_package pastix
petsc_opts=-ksp_view 

one_file_one_node: one_file_one_node.o  chkopts
	-${CLINKER} -o one_file_one_node one_file_one_node.o  ${PETSC_KSP_LIB}
	${RM} one_file_one_node.o

multi_file_multi_node: multi_file_multi_node.o  chkopts
	-${CLINKER} -o multi_file_multi_node multi_file_multi_node.o  ${PETSC_KSP_LIB}
	${RM} multi_file_multi_node.o

run_one_file_one_node:
	${MPIEXEC} ${mpi_opts_one} ./one_file_one_node -f /allen/programs/celltypes/workgroups/em-connectomics/danielk/EM_aligner_python/tmp_output_one/1020_1020.h5 ${pastix_opts} ${petsc_opts}

run_multi_file_multi_node:
	${MPIEXEC} ${mpi_opts} ./multi_file_multi_node -f /allen/programs/celltypes/workgroups/em-connectomics/danielk/EM_aligner_python/tmp_output/index.txt ${pastix_opts} ${petsc_opts} -mat_view ::ascii_info

run_valgrind:
	${MPIEXEC} ${mpi_opts} valgrind --leak-check=yes --suppressions=./valg.supp ./one_file_one_node -f /allen/programs/celltypes/workgroups/em-connectomics/danielk/EM_aligner_python/tmp_output/index.txt ${pastix_opts} ${petsc_opts}
	#mpiexec -N 1 ./one_file_one_node -f ../tmp_output/1020_1021.h5 ${pastix_opts} ${petsc_opts}

