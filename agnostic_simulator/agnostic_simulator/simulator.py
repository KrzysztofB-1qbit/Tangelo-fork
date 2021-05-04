"""
    Simulator class, wrapping around the various simulators and abstracting their differences from the user.
    Able to run noiseless and noisy simulations, leveraging the capabilities of different backends, quantum or
    classical.

    If the user provides a noise model, then a noisy simulation is run with n_shots shots.
    If the user only provides n_shots, a noiseless simulation is run, drawing the desired amount of shots.
    If the target backend has access to the statevector representing the quantum state, we leverage any kind of
    emulation available to reduce runtime (directly generating shot values from final statevector etc)
    If the quantum circuit contains a MEASURE instruction, it is assumed to simulate a mixed-state and the simulation
    will be carried by simulating individual shots (e.g a number of shots is required).

    Some backends may only support a subset of the above. This information is contained in a separate data-structure
"""

import os
import re
import numpy as np
from scipy import stats
from bitarray import bitarray
from collections import Counter

import qulacs
import qiskit
from projectq import MainEngine
from projectq.ops import *

from agnostic_simulator import Gate, Circuit
from agnostic_simulator.helpers import measurement_basis_gates
import agnostic_simulator.translator as translator

# Data-structure showing what functionalities are supported by the backend, in this package
backend_info = dict()
backend_info["qiskit"] = {"statevector_available": True, "statevector_order": "msq_first", "noisy_simulation": True}
backend_info["qulacs"] = {"statevector_available": True, "statevector_order": "msq_first", "noisy_simulation": True}
backend_info["projectq"] = {"statevector_available": True, "statevector_order": "msq_first", "noisy_simulation": False}
backend_info["qdk"] = {"statevector_available": False, "statevector_order": None, "noisy_simulation": False}


class Simulator:

    def __init__(self, target="qulacs", n_shots=None, noise_model=None):
        """
            Instantiate Simulator object.

            Args:
                target (str): One of the available target backends (quantum or classical)
                n_shots (int): Number of shots if using a shot-based simulator
                noise_model: A noise model object assumed to be in the format expected from the target backend
        """
        self._source = "abstract"
        self._target = target
        self._current_state = None
        self._noise_model = noise_model

        # Can be modified later by user as long as long as it retains the same type (ex: cannot change to/from None)
        self.n_shots = n_shots
        self.freq_threshold = 1e-10

        # Set additional attributes related to the target backend chosen by the user
        for k, v in backend_info[self._target].items():
            setattr(self, k, v)

        # Raise error if user attempts to pass a noise model to a backend not supporting noisy simulation
        if self._noise_model and not self.noisy_simulation:
            raise ValueError("Target backend does not support noise models.")

        # Raise error if the number of shots has not been passed for a noisy simulation or if statevector unavailable
        if not self.n_shots and (not self.statevector_available or self._noise_model):
            raise ValueError("A number of shots needs to be specified.")

    def simulate(self, source_circuit, return_statevector=False, initial_statevector=None):
        """
            Perform state preparation corresponding to the input circuit on the target backend, return the
            frequencies of the different observables, and either the statevector or None depending on
            the availability of the statevector and if return_statevector is set to True.
            For the statevector backends supporting it, an initial statevector can be provided to initialize the
            quantum state without simulating all the eauivalent gates.

            Args:
                source_circuit: a circuit in the abstract format to be translated for the target backend
                return_statevector(bool): option to return the statevector as well, if available
                initial_statevector(list/array) : A valid statevector in the format supported by the target backend

            Returns:
                A tuple containing a dictionary mapping multi-qubit states to their corresponding frequency, and
                the statevector, if available for the target backend and requested by the user (if not, set to None).
        """

        if source_circuit.is_mixed_state and not self.n_shots:
            raise ValueError("Circuit contains MEASURE instruction, and is assumed to prepare a mixed state."
                             "Please set the Simulator.n_shots attribute to an appropriate value.")

        if source_circuit.width == 0:
            raise ValueError("Cannot simulate an empty circuit (e.g identity unitary) with unknown number of qubits.")

        # If the unitary is the identity (no gates), no need for simulation: return all-zero state
        if source_circuit.size == 0:
            frequencies = {'0'*source_circuit.width: 1.0}
            statevector = np.zeros(2**source_circuit.width); statevector[0] =1.0
            return (frequencies, statevector) if return_statevector else (frequencies, None)

        if self._target == "qulacs":

            translated_circuit = translator.translate_qulacs(source_circuit, self._noise_model)

            # Initialize state on GPU if available and desired. Default to CPU otherwise.
            if ('QuantumStateGpu' in dir(qulacs)) and (int(os.getenv("QULACS_USE_GPU", 0)) != 0):
                state = qulacs.QuantumStateGpu(source_circuit.width)
            else:
                state = qulacs.QuantumState(source_circuit.width)
            if initial_statevector is not None:
                state.load(initial_statevector)

            samples = list()
            shots = self.n_shots if (source_circuit.is_mixed_state or self._noise_model) else 1
            for i in range(shots):

                translated_circuit.update_quantum_state(state)
                if source_circuit.is_mixed_state or self._noise_model:
                    samples.append(state.sampling(1)[0])
                    if initial_statevector:
                        state.load(initial_statevector)
                    else:
                        state.set_zero_state()
                else:
                    self._current_state = state
                    python_statevector = state.get_vector()
                    frequencies = self._statevector_to_frequencies(python_statevector)
                    return (frequencies, np.array(python_statevector)) if return_statevector else (frequencies, None)

            frequencies = {self.__int_to_binstr(k, source_circuit.width): v / self.n_shots
                           for k, v in Counter(samples).items()}
            return (frequencies, None)

        elif self._target == "qiskit":
            translated_circuit = translator.translate_qiskit(source_circuit)

            # If requested, set initial state
            if initial_statevector is not None:
                if self._noise_model:
                    raise ValueError("Cannot load an initial state if using a noise model, with Qiskit")
                else:
                    n_qubits = int(math.log2(len(initial_statevector)))
                    initial_state_circuit = qiskit.QuantumCircuit(n_qubits, n_qubits)
                    initial_state_circuit.initialize(initial_statevector, list(range(n_qubits)))
                    translated_circuit = initial_state_circuit + translated_circuit

            # Drawing individual shots with the qasm simulator, for noisy simulation or simulating mixed states
            if self._noise_model or source_circuit.is_mixed_state:
                from agnostic_simulator.noisy_simulation.noise_models import get_qiskit_noise_model

                meas_range = range(source_circuit.width)
                translated_circuit.measure(meas_range, meas_range)
                return_statevector = False
                backend = qiskit.Aer.get_backend("qasm_simulator")

                qiskit_noise_model = get_qiskit_noise_model(self._noise_model) if self._noise_model else None
                opt_level = 0 if self._noise_model else None

                job_sim = qiskit.execute(translated_circuit, backend, noise_model=qiskit_noise_model,
                                         shots=self.n_shots, basis_gates=None, optimization_level=opt_level)
                sim_results = job_sim.result()
                frequencies = {state[::-1]: count/self.n_shots for state, count in sim_results.get_counts(0).items()}

            # Noiseless simulation using the statevector simulator otherwise
            else:
                backend = qiskit.Aer.get_backend("statevector_simulator")
                job_sim = qiskit.execute(translated_circuit, backend)
                sim_results = job_sim.result()
                self._current_state = sim_results.get_statevector()
                frequencies = self._statevector_to_frequencies(self._current_state)

            return (frequencies, np.array(sim_results.get_statevector())) if return_statevector else (frequencies, None)

        elif self._target == "projectq":

            translated_circuit = translator.translate_projectq(source_circuit)
            translated_circuit = re.sub(r'(.*)llocate(.*)\n', '', translated_circuit)
            all_zero_state = np.zeros(2 ** source_circuit.width, dtype=np.complex);  all_zero_state[0] = 1.0

            eng = MainEngine()
            if initial_statevector is not None:
                Qureg = eng.allocate_qureg(int(math.log2(len(initial_statevector))))
                eng.flush()
                eng.backend.set_wavefunction(initial_statevector, Qureg)
            else:
                Qureg = eng.allocate_qureg(source_circuit.width)
            eng.flush()

            samples = list()
            shots = self.n_shots if source_circuit.is_mixed_state else 1
            for i in range(shots):
                exec(translated_circuit)
                eng.flush()

                if source_circuit.is_mixed_state:
                    All(Measure) | Qureg
                    eng.flush()
                    sample = ''.join([str(int(q)) for q in Qureg])
                    samples.append(sample)
                    initial_state = initial_statevector if initial_statevector else all_zero_state
                    eng.backend.set_wavefunction(initial_state, Qureg)
                else:
                    self._current_state = eng.backend.cheat()[1]
                    statevector = eng.backend.cheat()[1]
                    All(Measure) | Qureg
                    eng.flush()
                    frequencies = self._statevector_to_frequencies(self._current_state)
                    return (frequencies, np.array(statevector)) if return_statevector else (frequencies, None)

            frequencies = {k: v / self.n_shots for k, v in Counter(samples).items()}
            return (frequencies, None)

        elif self._target == "qdk":

            translated_circuit = translator.translate_qsharp(source_circuit)
            with open('tmp_circuit.qs', 'w+') as f_out:
                f_out.write(translated_circuit)

            # Compile, import and call Q# operation to compute frequencies. Only import qsharp module if qdk is running
            # TODO: A try block to catch an exception at compile time, for Q#? Probably as an ImportError.
            import qsharp
            qsharp.reload()
            from MyNamespace import EstimateFrequencies
            frequencies_list = EstimateFrequencies.simulate(nQubits=source_circuit.width, nShots=self.n_shots)
            print("Q# frequency estimation with {0} shots: \n {1}".format(self.n_shots, frequencies_list))

            # Convert Q# output to frequency dictionary, apply threshold
            frequencies = {bin(i).split('b')[-1]: frequencies_list[i] for i, freq in enumerate(frequencies_list)}
            frequencies = {("0"*(source_circuit.width-len(k))+k)[::-1]: v for k, v in frequencies.items()
                           if v > self.freq_threshold}
            return (frequencies, None)

    def get_expectation_value(self, qubit_operator, state_prep_circuit):
        """
            Take as input a qubit operator H and a quantum circuit preparing a state |\psi>
            Return the expectation value <\psi | H | \psi>

            In the case of a noiseless simulation, if the target backend exposes the statevector
            then it is used directly to compute expectation values, or draw samples if required.
            In the case of a noisy simulator, or if the statevector is not available on the target backend, individual
            shots must be run and the workflow is akin to what we would expect from an actual QPU.

            Args:
                qubit_operator(openfermion-style QubitOperator class): a qubit operator
                state_prep_circuit: an abstract circuit used for state preparation

            Returns:
                The expectation value of this operator with regards to the state preparation
        """

        # Check that qubit operator does not operate on qubits beyond circuit size
        # Forces coefficients to be real numbers (Openfermion stores them as complex numbers although they are real)
        for term, coef in qubit_operator.terms.items():
            if state_prep_circuit.width < len(term):
                raise ValueError(f'Term {term} requires more qubits than the circuit contains ({state_prep_circuit.width})')
            qubit_operator.terms[term] = coef.real

        if self._noise_model or not self.statevector_available \
                or state_prep_circuit.is_mixed_state or state_prep_circuit.size == 0:
            return self._get_expectation_value_from_frequencies(qubit_operator, state_prep_circuit)
        elif self.statevector_available:
            return self._get_expectation_value_from_statevector(qubit_operator, state_prep_circuit)

    def _get_expectation_value_from_statevector(self, qubit_operator, state_prep_circuit):
        """
            Take as input a qubit operator H and a state preparation returning a ket |\psi>.
            Return the expectation value <\psi | H | \psi>, computed without drawing samples (statevector only)

            Args:
                qubit_operator(openfermion-style QubitOperator class): a qubit operator
                state_prep_circuit: an abstract circuit used for state preparation (only pure states)

            Returns:
                The expectation value of this operator with regards to the state preparation
        """
        n_qubits = state_prep_circuit.width

        expectation_value = 0.
        prepared_frequencies, prepared_state = self.simulate(state_prep_circuit, return_statevector=True)

        # Use fast built-in qulacs expectation value function if possible
        if self._target == "qulacs" and not self.n_shots:
            op = qulacs.quantum_operator.create_quantum_operator_from_openfermion_text(qubit_operator.__repr__())
            if op.get_qubit_count() == n_qubits:
                return op.get_expectation_value(self._current_state).real
            else:
                operator = qulacs.GeneralQuantumOperator(n_qubits)
                for i in range(op.get_term_count()):
                    operator.add_operator(op.get_term(i))
                return operator.get_expectation_value(self._current_state).real

        # Use fast built-in projectq expectation value function if possible
        if self._target == "projectq" and not self.n_shots:
            eng = MainEngine()
            Qureg = eng.allocate_qureg(n_qubits)
            eng.flush()
            eng.backend.set_wavefunction(prepared_state, Qureg)
            eng.flush()
            exp_value = eng.backend.get_expectation_value(qubit_operator, Qureg)
            All(Measure) | Qureg
            eng.flush()
            return exp_value

        # Otherwise, use generic statevector expectation value
        for term, coef in qubit_operator.terms.items():

            if len(term) > n_qubits:  # Cannot have a qubit index beyond circuit size
                raise ValueError(f"Size of operator {qubit_operator} beyond circuit width ({n_qubits} qubits)")
            elif not term:  # Empty term: no simulation needed
                expectation_value += coef
                continue

            if not self.n_shots:
                # Directly simulate and compute expectation value using statevector
                pauli_circuit = Circuit([Gate(pauli, index) for index, pauli in term], n_qubits=n_qubits)
                _, pauli_state = self.simulate(pauli_circuit, return_statevector=True, initial_statevector=prepared_state)

                delta = 0.
                for i in range(len(prepared_state)):
                    delta += pauli_state[i].real * prepared_state[i].real + pauli_state[i].imag * prepared_state[i].imag
                expectation_value += coef * delta

            else:
                # Run simulation with statevector but compute expectation value with samples directly drawn from it
                basis_circuit = Circuit(measurement_basis_gates(term))
                if basis_circuit.size > 0:
                    frequencies, _ = self.simulate(basis_circuit, initial_statevector=prepared_state)
                else:
                    frequencies = prepared_frequencies
                expectation_term = self.get_expectation_value_from_frequencies_oneterm(term, frequencies)
                expectation_value += coef * expectation_term

        return expectation_value

    def _get_expectation_value_from_frequencies(self, qubit_operator, state_prep_circuit):
        """
            Take as input a qubit operator H and a state preparation returning a ket |\psi>.
            Return the expectation value <\psi | H | \psi> computed using the frequencies of observable states.

            Args:
                qubit_operator(openfermion-style QubitOperator class): a qubit operator
                state_prep_circuit: an abstract circuit used for state preparation

            Returns:
                The expectation value of this operator with regards to the state preparation
        """
        n_qubits = state_prep_circuit.width

        expectation_value = 0.
        for term, coef in qubit_operator.terms.items():

            if len(term) > n_qubits:
                raise ValueError(f"Size of operator {qubit_operator} beyond circuit width ({n_qubits} qubits)")
            elif not term: # Empty term: no simulation needed
                expectation_value += coef
                continue

            basis_circuit = Circuit(measurement_basis_gates(term))
            full_circuit = state_prep_circuit + basis_circuit if (basis_circuit.size > 0) else state_prep_circuit
            frequencies, _ = self.simulate(full_circuit)

            expectation_term = self.get_expectation_value_from_frequencies_oneterm(term, frequencies)
            expectation_value += coef * expectation_term

        return expectation_value

    @staticmethod
    def get_expectation_value_from_frequencies_oneterm(term, frequencies):
        """
            Return the expectation value of a single-term qubit-operator, given the result of a state-preparation

            Args:
                term(openfermion-style QubitOperator object): a qubit operator, with only a single term
                frequencies(dict): histogram of frequencies of measurements (assumed to be in lsq-first format)

            Returns:
                The expectation value of this operator with regards to the state preparation
        """

        if not frequencies.keys():
            return ValueError("Must pass a non-empty dictionary of frequencies.")
        n_qubits = len(list(frequencies.keys())[0])

        # Get term mask
        mask = ["0"] * n_qubits
        for index, op in term:
            mask[index] = "1"
        mask = "".join(mask)

        # Compute expectation value of the term
        expectation_term = 0.
        for basis_state, freq in frequencies.items():
            # Compute sample value using state_binstr and term mask, update term expectation value
            sample = (-1) ** ((bitarray(mask) & bitarray(basis_state)).to01().count("1") % 2)
            expectation_term += sample * freq

        return expectation_term

    def _statevector_to_frequencies(self, statevector):
        """
            For a given statevector representing the quantum state of a qubit register, returns a sparse histogram
            of the probabilities in the least-significant-qubit (lsq) -first order.
            e.g the string '100' means qubit 0 measured in basis state |1>, and qubit 1 & 2 both measured in state |0>

            Args:
                statevector(list or ndarray(complex)): an iterable 1D data-structure containing the amplitudes

            Returns:
                A dictionary whose keys are bitstrings representing the multi-qubit states with the least significant
                qubit first (e.g '100' means qubit 0 in state |1>, and qubit 1 and 2 in state |0>), and the associated
                value is the corresponding frequency. Unless threshold=0., this dictionary will be sparse.
        """

        n_qubits = int(math.log2(len(statevector)))
        frequencies = dict()
        for i, amplitude in enumerate(statevector):
            frequency = abs(amplitude)**2
            if (frequency - self.freq_threshold) >= 0.:
                frequencies[self.__int_to_binstr(i, n_qubits)] = frequency

        # If n_shots, has been specified, then draw that amount of samples from the distribution
        # and return empirical frequencies instead. Otherwise, return the exact frequencies
        if not self.n_shots:
            return frequencies
        else:
            xk, pk = [], []
            for k, v in frequencies.items():
                xk.append(int(k[::-1], 2))
                pk.append(frequencies[k])

            distr = stats.rv_discrete(name='distr', values=(np.array(xk), np.array(pk)))
            samples = distr.rvs(size=self.n_shots)
            freqs_shots = {self.__int_to_binstr(k, n_qubits): v / self.n_shots for k, v in Counter(samples).items()}
            return freqs_shots

    def __int_to_binstr(self, i, n_qubits):
        """ Convert an integer into a bit string of size n_qubits, in the order specified for the state vector """
        bs = bin(i).split('b')[-1]
        state_binstr = "0" * (n_qubits - len(bs)) + bs
        return state_binstr[::-1] if (self.statevector_order == "msq_first") else state_binstr