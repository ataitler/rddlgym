import gurobipy
from gurobipy import GRB
import math
from typing import Dict, List, Tuple

from pyRDDLGym.Core.ErrorHandling.RDDLException import print_stack_trace
from pyRDDLGym.Core.ErrorHandling.RDDLException import RDDLNotImplementedError
from pyRDDLGym.Core.ErrorHandling.RDDLException import RDDLUndefinedVariableError

from pyRDDLGym.Core.Compiler.RDDLLevelAnalysis import RDDLLevelAnalysis
from pyRDDLGym.Core.Compiler.RDDLLiftedModel import RDDLLiftedModel
from pyRDDLGym.Core.Compiler.RDDLObjectsTracer import RDDLObjectsTracer
from pyRDDLGym.Core.Compiler.RDDLValueInitializer import RDDLValueInitializer
from pyRDDLGym.Core.Debug.Logger import Logger
from pyRDDLGym.Core.Grounder.RDDLGrounder import RDDLGrounder
from pyRDDLGym.Core.Gurobi.GurobiRDDLPlan import GurobiRDDLPlan


class GurobiRDDLCompiler:
    
    def __init__(self, rddl: RDDLLiftedModel,
                 plan: GurobiRDDLPlan,
                 allow_synchronous_state: bool=True,
                 rollout_horizon: int=None,
                 epsilon: float=1e-6,
                 float_range: Tuple[float, float]=(1e-15, 1e15),
                 model_params: Dict[str, object]={'NonConvex': 2},
                 piecewise_options: str='',
                 logger: Logger=None) -> None:
        '''Creates a new compiler from RDDL model to Gurobi problem.
        
        :param rddl: the RDDL model
        :param plan: the plan or policy to optimize
        :param allow_synchronous_state: whether state-fluent can be synchronous
        :param rollout_horizon: length of the planning horizon (uses the RDDL
        defined horizon if None)
        :param epsilon: small positive constant used for comparing equality of
        real numbers in Gurobi constraints
        :param float_range: range of floating values that can be passed to 
        Gurobi to initialize fluents and non-fluents (values outside this range
        are clipped)
        :param model_params: dictionary of parameter name and values to
        pass to Gurobi model after compilation
        :param piecewise_options: a string of parameters to pass to Gurobi
        "options" parameter when creating constraints that contain piecewise
        linear approximations (e.g. cos, log, exp)
        :param logger: to log information about compilation to file
        '''
        self.plan = plan
        if rollout_horizon is None:
            rollout_horizon = rddl.horizon
        self.horizon = rollout_horizon
        self.allow_synchronous_state = allow_synchronous_state
        self.logger = logger
        
        # Gurobi-specific parameters
        self.epsilon = epsilon
        self.float_range = float_range
        self.model_params = model_params
        self.pw_options = piecewise_options
        
        # type conversion to Gurobi
        self.GUROBI_TYPES = {
            'int': GRB.INTEGER,
            'real': GRB.CONTINUOUS,
            'bool': GRB.BINARY
        }
        
        # ground out the domain
        grounder = RDDLGrounder(rddl._AST)
        self.rddl = grounder.Ground()
        
        # compile initial values
        if self.logger is not None:
            self.logger.clear()
        initializer = RDDLValueInitializer(self.rddl, logger=self.logger)
        self.init_values = initializer.initialize()
        
        # compute dependency graph for CPFs and sort them by evaluation order
        sorter = RDDLLevelAnalysis(
            self.rddl, allow_synchronous_state, logger=self.logger)
        self.levels = sorter.compute_levels()     
        
        # trace expressions to cache information to be used later
        tracer = RDDLObjectsTracer(self.rddl, logger=self.logger)
        self.traced = tracer.trace()
    
    def solve(self, init_values: Dict[str, object]=None) -> List[Dict[str, object]]:
        '''Compiles the current RDDL domain into a Gurobi problem and solves the
        problem. An optimal solution is returned in RDDL format.
        
        :param init_values: override the initial values of fluents and
        non-fluents as defined in the RDDL file (if None, then the original
        values defined in the RDDL domain + instance are used instead)
        '''
        model, all_action_vars = self._compile(init_values)
        model.optimize()
        return self._get_optimal_actions(all_action_vars)
    
    def _get_optimal_actions(self, action_vars):
        rddl = self.rddl
        optimal_plan = []
        for actions_vars in action_vars:
            optimal_plan.append({})
            for (name, (var, *_)) in actions_vars.items():
                prange = rddl.actionsranges[name]
                action = var.X
                if prange == 'int':
                    action = int(action)
                elif prange == 'bool':
                    action = (action > 0.5)
                optimal_plan[-1][name] = action
        return optimal_plan
    
    # ===========================================================================
    # main compilation subroutines
    # ===========================================================================
    
    def _rollout(self, model, plan, params, subs):
        objective = 0
        all_action_vars = []
        for step in range(self.horizon):
            
            # add action fluent variables to model
            action_vars = plan.predict(self, model, params, step, subs)
            all_action_vars.append(action_vars)
            subs.update(action_vars)
            
            # add action constraints
            self._compile_maxnondef_constraint(model, subs)
            self._compile_action_preconditions(model, subs)
            
            # evaluate CPFs and reward
            self._compile_cpfs(model, subs)
            reward = self._compile_reward(model, subs)
            objective += reward
            
            # update state
            for (state, next_state) in self.rddl.next_state.items():
                subs[state] = subs[next_state]
        return objective, all_action_vars
    
    def _compile(self, init_values: Dict[str, object]=None) -> \
        Tuple[gurobipy.Model, List[Dict[str, object]]]:
        '''Compiles and returns the current RDDL domain as a Gurobi optimization
        problem. Also returns action variables 
        
        :param init_values: override the initial values of fluents and
        non-fluents as defined in the RDDL file (if None, then the original
        values defined in the RDDL domain + instance are used instead)
        '''
        model = self._create_model()
        params = self.plan.parameterize(self, model)
        subs = self._compile_init_subs(init_values)  
        objective, all_action_vars = self._rollout(model, self.plan, params, subs)
        model.setObjective(objective, GRB.MAXIMIZE)
        return model, all_action_vars
    
    def _create_model(self) -> gurobipy.Model:
        
        # create the Gurobi optimization problem
        env = gurobipy.Env(empty=True)
        env.start()
        model = gurobipy.Model(env=env)
        
        # set additional model settings here before optimization
        for (name, value) in self.model_params.items():
            model.setParam(name, value)
        return model 
        
    def _compile_init_subs(self, init_values=None) -> Dict[str, object]:
        if init_values is None:
            init_values = self.init_values
        rddl = self.rddl
        smallest, largest = self.float_range
        subs = {}
        for (var, value) in init_values.items():
            prange = rddl.variable_ranges[var]
            vtype = self.GUROBI_TYPES[prange]
            safe_value = value
            if rddl.variable_ranges[var] == 'real':
                if 0 < value < smallest:
                    safe_value = smallest
                elif -smallest < value < 0:
                    safe_value = -smallest
                elif value > largest:
                    safe_value = largest
                elif value < -largest:
                    safe_value = -largest
            lb, ub = GurobiRDDLCompiler._fix_bounds(safe_value, safe_value)
            subs[var] = (safe_value, vtype, lb, ub, False)
        return subs
        
    def _compile_action_preconditions(self, model, subs) -> None:
        for precondition in self.rddl.preconditions:
            indicator, *_, symb = self._gurobi(precondition, model, subs)
            if symb:
                model.addConstr(indicator == 1)
    
    def _compile_maxnondef_constraint(self, model, subs) -> None:
        rddl = self.rddl
        num_bool, sum_bool = 0, 0
        for (action, prange) in rddl.actionsranges.items():
            if prange == 'bool':
                var, *_ = subs[action]
                num_bool += 1
                sum_bool += var
        if rddl.max_allowed_actions < num_bool:
            model.addConstr(sum_bool <= rddl.max_allowed_actions)
            
    def _compile_cpfs(self, model, subs) -> None:
        rddl = self.rddl
        for cpfs in self.levels.values():
            for cpf in cpfs:
                _, expr = rddl.cpfs[cpf]
                subs[cpf] = self._gurobi(expr, model, subs)
    
    def _compile_reward(self, model, subs) -> object:
        reward, *_ = self._gurobi(self.rddl.reward, model, subs)
        return reward
    
    # ===========================================================================
    # start of compilation subroutines
    # ===========================================================================
    
    # IMPORTANT: all helper methods below must return either a Gurobi variable
    # or a constant as the first argument
    def _gurobi(self, expr, model, subs):
        etype, _ = expr.etype
        if etype == 'constant':
            return self._gurobi_constant(expr, model, subs)
        elif etype == 'pvar':
            return self._gurobi_pvar(expr, model, subs)
        elif etype == 'arithmetic':
            return self._gurobi_arithmetic(expr, model, subs)
        elif etype == 'relational':
            return self._gurobi_relational(expr, model, subs)
        elif etype == 'boolean':
            return self._gurobi_logical(expr, model, subs)
        elif etype == 'func':
            return self._gurobi_function(expr, model, subs)
        elif etype == 'control':
            return self._gurobi_control(expr, model, subs)
        elif etype == 'randomvar':
            return self._gurobi_random(expr, model, subs)
        else:
            raise RDDLNotImplementedError(
                f'Expression type {etype} is not supported in Gurobi compiler.\n' + 
                print_stack_trace(expr))
            
    def _add_var(self, model, vtype, lb=-GRB.INFINITY, ub=GRB.INFINITY, name=''):
        '''Add a generic variable to the Gurobi model.'''
        return model.addVar(vtype=vtype, lb=lb, ub=ub, name=name)
    
    def _add_bool_var(self, model, name=''):
        '''Add a BINARY variable to the Gurobi model.'''
        return self._add_var(model, GRB.BINARY, 0, 1, name=name)
    
    def _add_real_var(self, model, lb=-GRB.INFINITY, ub=GRB.INFINITY, name=''):
        '''Add a CONTINUOUS variable to the Gurobi model.'''
        return self._add_var(model, GRB.CONTINUOUS, lb, ub, name=name)
    
    def _add_int_var(self, model, lb=-GRB.INFINITY, ub=GRB.INFINITY, name=''):
        '''Add a INTEGER variable to the Gurobi model.'''
        return self._add_var(model, GRB.INTEGER, lb, ub, name=name)
    
    # ===========================================================================
    # leaves
    # ===========================================================================
    
    @staticmethod
    def _fix_bounds(lb, ub):
        assert (not math.isnan(lb))
        assert (not math.isnan(ub))
        assert (ub >= lb)
        lb = max(min(lb, GRB.INFINITY), -GRB.INFINITY)
        ub = max(min(ub, GRB.INFINITY), -GRB.INFINITY)
        return lb, ub
        
    def _gurobi_constant(self, expr, model, subs):
        
        # get the cached value of this constant
        value = self.traced.cached_sim_info(expr)
        
        # infer type of value and assign to Gurobi type
        if isinstance(value, bool):
            vtype = GRB.BINARY
        elif isinstance(value, int):
            vtype = GRB.INTEGER
        elif isinstance(value, float):
            vtype = GRB.CONTINUOUS
        else:
            raise RDDLNotImplementedError(
                f'Range of {value} is not supported in Gurobi compiler.')
        
        # bounds form a singleton set containing the cached value
        lb, ub = GurobiRDDLCompiler._fix_bounds(value, value)
        return lb, vtype, lb, ub, False

    def _gurobi_pvar(self, expr, model, subs):
        var, _ = expr.args
        
        # domain object converted to canonical index
        is_value, value = self.traced.cached_sim_info(expr)
        if is_value:
            lb, ub = GurobiRDDLCompiler._fix_bounds(value, value)
            return lb, GRB.INTEGER, lb, ub, False
        
        # extract variable value
        value = subs.get(var, None)
        if value is None:
            raise RDDLUndefinedVariableError(
                f'Variable <{var}> is referenced before assignment.\n' + 
                print_stack_trace(expr))
        return value
    
    # ===========================================================================
    # arithmetic
    # ===========================================================================
    
    @staticmethod
    def _promote_vtype(vtype1, vtype2):
        if vtype1 == GRB.BINARY:
            return vtype2
        elif vtype2 == GRB.BINARY:
            return vtype1
        elif vtype1 == GRB.INTEGER:
            return vtype2
        elif vtype2 == GRB.INTEGER:
            return vtype1
        else:
            assert (vtype1 == vtype2 == GRB.CONTINUOUS)
            return vtype1
    
    @staticmethod
    def _at_least_int(vtype):
        return GurobiRDDLCompiler._promote_vtype(vtype, GRB.INTEGER)
    
    def _gurobi_arithmetic(self, expr, model, subs):
        _, op = expr.etype
        args = expr.args        
        n = len(args)
        
        # unary negation
        if n == 1 and op == '-':
            arg, = args
            gterm, vtype, lb, ub, symb = self._gurobi(arg, model, subs)
            vtype = GurobiRDDLCompiler._at_least_int(vtype)
            lb, ub = GurobiRDDLCompiler._fix_bounds(-ub, -lb)
            
            # assign negative to a new variable
            if symb: 
                res = self._add_var(model, vtype, lb, ub)
                model.addConstr(res == -gterm)
            else:
                res = -gterm
                lb, ub = res, res                
            return res, vtype, lb, ub, symb
        
        # binary operations
        elif n >= 1:
            
            # unwrap addition to binary operations
            if op == '+':
                arg, *rest = args
                res, vtype, lb, ub, symb = self._gurobi(arg, model, subs)
                vtype = GurobiRDDLCompiler._at_least_int(vtype)
                for arg in rest:
                    gterm, vtype1, lb2, ub2, symb2 = self._gurobi(arg, model, subs)
                    res = res + gterm
                    vtype = GurobiRDDLCompiler._promote_vtype(vtype, vtype1)
                    lb, ub = GurobiRDDLCompiler._fix_bounds(lb + lb2, ub + ub2)
                    symb = symb or symb2
                
                # assign sum to a new variable
                if symb:
                    newres = self._add_var(model, vtype, lb, ub)
                    model.addConstr(newres == res)
                else:
                    newres = res         
                    lb, ub = newres, newres                    
                return newres, vtype, lb, ub, symb
            
            # unwrap multiplication to binary operations
            elif op == '*':
                arg, *rest = args
                res, vtype, lb, ub, symb = self._gurobi(arg, model, subs)
                vtype = GurobiRDDLCompiler._at_least_int(vtype)
                for arg in rest:
                    gterm, vtype1, lb2, ub2, symb2 = self._gurobi(arg, model, subs)
                    res = res * gterm
                    vtype = GurobiRDDLCompiler._promote_vtype(vtype, vtype1)
                    
                    # update bounds for product
                    lbub = (lb * lb2, lb * ub2, ub * lb2, ub * ub2)
                    lb, ub = GurobiRDDLCompiler._fix_bounds(min(lbub), max(lbub))
                    symb = symb or symb2
                    
                # assign product to a new variable
                if symb: 
                    newres = self._add_var(model, vtype, lb, ub)
                    model.addConstr(newres == res)
                else:
                    newres = res
                    lb, ub = newres, newres                    
                return newres, vtype, lb, ub, symb
            
            # subtraction
            elif n == 2 and op == '-':
                arg1, arg2 = args
                gterm1, vtype1, lb1, ub1, symb1 = self._gurobi(arg1, model, subs)
                gterm2, vtype2, lb2, ub2, symb2 = self._gurobi(arg2, model, subs)
                vtype = GurobiRDDLCompiler._promote_vtype(vtype1, vtype2)
                vtype = GurobiRDDLCompiler._at_least_int(vtype)
                symb = symb1 or symb2
                
                # assign difference to a new variable
                if symb:
                    lb, ub = GurobiRDDLCompiler._fix_bounds(lb1 - ub2, ub1 - lb2)
                    res = self._add_var(model, vtype, lb, ub)
                    model.addConstr(res == gterm1 - gterm2)
                else:
                    res = gterm1 - gterm2
                    lb, ub = res, res                    
                return res, vtype, lb, ub, symb
            
            # quotient x / y
            elif n == 2 and op == '/':
                arg1, arg2 = args
                gterm1, _, lb1, ub1, symb1 = self._gurobi(arg1, model, subs)
                gterm2, _, lb2, ub2, symb2 = self._gurobi(arg2, model, subs)
                
                if symb2: 
                    # compute interval containing 1 / y
                    if 0 > lb2 and 0 < ub2:
                        lb2, ub2 = -GRB.INFINITY, GRB.INFINITY
                    elif lb2 == 0 and ub2 == 0:
                        lb2, ub2 = GRB.INFINITY, GRB.INFINITY
                    elif lb2 == 0:
                        lb2, ub2 = 1 / ub2, GRB.INFINITY
                    elif ub2 == 0:
                        lb2, ub2 = -GRB.INFINITY, 1 / lb2
                    else:
                        lb2, ub2 = 1 / ub2, 1 / lb2
                    lb2, ub2 = GurobiRDDLCompiler._fix_bounds(lb2, ub2)
                    
                    # implement z = 1 / y as a constraint z * y = 1
                    recip2 = self._add_real_var(model, lb2, ub2)
                    model.addConstr(recip2 * gterm2 == 1)
                else:
                    recip2 = 1 / gterm2
                    lb2, ub2 = recip2, recip2
                
                symb = symb1 or symb2
                if symb:
                    # compute interval containing x / y
                    lbub = (lb1 * lb2, lb1 * ub2, ub1 * lb2, ub1 * ub2)
                    lb, ub = GurobiRDDLCompiler._fix_bounds(min(lbub), max(lbub))
                    
                    # finally compute x / y = x * (1 / y)
                    res = self._add_real_var(model, lb, ub)
                    model.addConstr(res == gterm1 * recip2)
                else:
                    res = gterm1 * recip2
                    lb, ub = res, res                    
                return res, GRB.CONTINUOUS, lb, ub, symb
        
        raise RDDLNotImplementedError(
            f'Arithmetic operator {op} with {n} arguments is not '
            f'supported in Gurobi compiler.\n' + 
            print_stack_trace(expr))
    
    # ===========================================================================
    # boolean
    # ===========================================================================
    
    def _gurobi_relational(self, expr, model, subs):
        _, op = expr.etype
        args = expr.args        
        n = len(args)
        
        if n == 2:
            lhs, rhs = args
            glhs, _, lb1, ub1, symb1 = self._gurobi(lhs, model, subs)
            grhs, _, lb2, ub2, symb2 = self._gurobi(rhs, model, subs)
            
            # convert <= to >=, < to >, etc.
            if op == '<=' or op == '<':
                glhs, grhs = grhs, glhs
                op = '>=' if op == '<=' else '>'
            
            # assign comparison operator to binary variable
            diff = glhs - grhs
            symb = symb1 or symb2
            if op == '==': 
                if symb:
                    # calculate absolute difference
                    lb, ub = lb1 - ub2, ub1 - lb2
                    if lb >= 0:
                        lb, ub = lb, ub
                    elif ub <= 0:
                        lb, ub = -ub, -lb
                    else:
                        lb, ub = 0, max(abs(lb), abs(ub))
                    lb, ub = GurobiRDDLCompiler._fix_bounds(lb, ub)
                    abs_diff = self._add_real_var(model, lb=lb, ub=ub)
                    model.addGenConstrAbs(abs_diff, diff)
                    
                    res = self._add_bool_var(model)
                    model.addConstr((res == 1) >> (abs_diff <= +self.epsilon))
                    model.addConstr((res == 0) >> (abs_diff >= 2 * self.epsilon))
                else:
                    res = glhs == grhs
                return res, GRB.BINARY, 0, 1, symb
            
            elif op == '>=':
                if symb:
                    res = self._add_bool_var(model)
                    model.addConstr((res == 1) >> (diff >= 0))
                    model.addConstr((res == 0) >> (diff <= -self.epsilon))
                else:
                    res = glhs >= grhs
                return res, GRB.BINARY, 0, 1, symb
            
            elif op == '~=':
                if symb:
                    # calculate absolute difference
                    lb, ub = lb1 - ub2, ub1 - lb2
                    if lb >= 0:
                        lb, ub = lb, ub
                    elif ub <= 0:
                        lb, ub = -ub, -lb
                    else:
                        lb, ub = 0, max(abs(lb), abs(ub))
                    lb, ub = GurobiRDDLCompiler._fix_bounds(lb, ub)
                    abs_diff = self._add_real_var(model, lb=lb, ub=ub)
                    model.addGenConstrAbs(abs_diff, diff)
                    
                    res = self._add_bool_var(model)
                    model.addConstr((res == 1) >> (abs_diff >= 2 * self.epsilon))
                    model.addConstr((res == 0) >> (abs_diff <= +self.epsilon))
                else: 
                    res = glhs != grhs
                return res, GRB.BINARY, 0, 1, symb
            
            elif op == '>':
                if symb:
                    res = self._add_bool_var(model)
                    model.addConstr((res == 1) >> (diff >= +self.epsilon))
                    model.addConstr((res == 0) >> (diff <= 0))
                else:
                    res = glhs > grhs
                return res, GRB.BINARY, 0, 1, symb
            
        raise RDDLNotImplementedError(
            f'Relational operator {op} with {n} arguments is not '
            f'supported in Gurobi compiler.\n' + 
            print_stack_trace(expr))
    
    def _gurobi_logical(self, expr, model, subs):
        _, op = expr.etype
        if op == '&':
            op = '^'
        args = expr.args        
        n = len(args)
        
        # unary negation ~z of z is a variable y such that y + z = 1
        if n == 1 and op == '~':
            arg, = args
            gterm, *_, symb = self._gurobi(arg, model, subs)
            if symb:
                res = self._add_bool_var(model)
                model.addConstr(res + gterm == 1)
            else:
                res = not gterm                
            return res, GRB.BINARY, 0, 1, symb
            
        # binary operations
        elif n >= 1:
            results = [self._gurobi(arg, model, subs) for arg in args]
            gterms = [result[0] for result in results]
            symbs = [result[-1] for result in results]
            symb = any(symbs)
            
            # any non-variables must be converted to variables
            if symb:
                for (i, gterm) in enumerate(gterms):
                    if not symbs[i]:
                        var = self._add_bool_var(model)
                        model.addConstr(var == gterm)
                        gterms[i] = var
                        symbs[i] = True
            
            # unwrap AND to binary operations
            if op == '^':
                if symb:
                    res = self._add_bool_var(model)
                    model.addGenConstrAnd(res, gterms)
                else:
                    res = all(gterms)                    
                return res, GRB.BINARY, 0, 1, symb
            
            # unwrap OR to binary operations
            elif op == '|':
                if symb:
                    res = self._add_bool_var(model)
                    model.addGenConstrOr(res, gterms)
                else:
                    res = any(gterms)                    
                return res, GRB.BINARY, 0, 1, symb
        
        raise RDDLNotImplementedError(
            f'Logical operator {op} with {n} arguments is not '
            f'supported in Gurobi compiler.\n' + 
            print_stack_trace(expr))
    
    # ===========================================================================
    # function
    # ===========================================================================

    @staticmethod
    def GRB_log(x):
        if x <= 0:
            return -GRB.INFINITY
        else:
            return math.log(x)
    
    def _gurobi_function(self, expr, model, subs):
        _, name = expr.etype
        args = expr.args
        n = len(args)
        
        # unary functions
        if n == 1:
            arg, = args
            gterm, vtype, lb, ub, symb = self._gurobi(arg, model, subs)
            vtype = GurobiRDDLCompiler._at_least_int(vtype)
            
            if name == 'abs': 
                if symb:
                    # compute bounds for abs
                    if lb >= 0:
                        lb, ub = lb, ub
                    elif ub <= 0:
                        lb, ub = -ub, -lb
                    else:
                        lb, ub = 0, max(abs(lb), abs(ub))
                    lb, ub = GurobiRDDLCompiler._fix_bounds(lb, ub)
                    
                    res = self._add_var(model, vtype, lb, ub)
                    model.addGenConstrAbs(res, gterm)                    
                else:
                    res = abs(gterm)
                    lb, ub = res, res                    
                return res, vtype, lb, ub, symb
            
            elif name == 'floor':
                if symb:
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        math.floor(lb), math.floor(ub))
                    res = self._add_int_var(model, lb, ub)
                    model.addConstr(res <= gterm)
                    model.addConstr(res + 1 >= gterm + self.epsilon)                    
                else:
                    res = int(math.floor(gterm))
                    lb, ub = res, res   
                return res, GRB.INTEGER, lb, ub, symb
            
            elif name == 'ceil':
                if symb:
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        math.ceil(lb), math.ceil(ub))
                    res = self._add_int_var(model, lb, ub)
                    model.addConstr(res >= gterm)
                    model.addConstr(res + 1 <= gterm - self.epsilon)
                else:
                    res = int(math.ceil(gterm))
                    lb, ub = res, res   
                return res, GRB.INTEGER, lb, ub, symb
                
            elif name == 'cos':
                if symb:
                    lb, ub = -1.0, 1.0
                    res = self._add_real_var(model, lb, ub)
                    model.addGenConstrCos(gterm, res, options=self.pw_options)
                else:
                    res = math.cos(gterm)
                    lb, ub = res, res                    
                return res, GRB.CONTINUOUS, lb, ub, symb
            
            elif name == 'sin':
                if symb:
                    lb, ub = -1.0, 1.0
                    res = self._add_real_var(model, lb, ub)
                    model.addGenConstrSin(gterm, res, options=self.pw_options)
                else:
                    res = math.sin(gterm)
                    lb, ub = res, res                    
                return res, GRB.CONTINUOUS, lb, ub, symb
            
            elif name == 'tan':
                if symb:
                    lb, ub = -GRB.INFINITY, GRB.INFINITY
                    res = self._add_real_var(model, lb, ub)
                    model.addGenConstrTan(gterm, res, options=self.pw_options)
                else:
                    res = math.tan(gterm)
                    lb, ub = res, res                    
                return res, GRB.CONTINUOUS, lb, ub, symb
            
            elif name == 'exp':
                if symb: 
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        math.exp(lb), math.exp(ub))
                    res = self._add_real_var(model, lb, ub)
                    model.addGenConstrExp(gterm, res, options=self.pw_options)
                else:
                    res = math.exp(gterm)
                    lb, ub = res, res                    
                return res, GRB.CONTINUOUS, lb, ub, symb
            
            elif name == 'ln': 
                if symb: 
                    # argument must be non-negative
                    lb, ub = max(lb, 0), max(ub, 0)
                    arg = self._add_var(model, vtype, lb, ub)
                    model.addGenConstrMax(arg, [gterm], constant=0)
                    
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        GurobiRDDLCompiler.GRB_log(lb),
                        GurobiRDDLCompiler.GRB_log(ub))
                    res = self._add_real_var(model, lb, ub)
                    model.addGenConstrLog(arg, res, options=self.pw_options)
                else:
                    res = math.log(gterm)
                    lb, ub = res, res                    
                return res, GRB.CONTINUOUS, lb, ub, symb
            
            elif name == 'sqrt':
                if symb: 
                    # argument must be non-negative
                    lb, ub = max(lb, 0), max(ub, 0)
                    arg = self._add_var(model, vtype, lb, ub)
                    model.addGenConstrMax(arg, [gterm], constant=0)
                    
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        math.sqrt(lb), math.sqrt(ub))
                    res = self._add_real_var(model, lb, ub)
                    model.addGenConstrPow(arg, res, 0.5, options=self.pw_options)
                else:
                    res = math.sqrt(gterm)
                    lb, ub = res, res                    
                return res, GRB.CONTINUOUS, lb, ub, symb
        
        # binary functions
        elif n == 2:
            arg1, arg2 = args
            gterm1, vtype1, lb1, ub1, symb1 = self._gurobi(arg1, model, subs)
            gterm2, vtype2, lb2, ub2, symb2 = self._gurobi(arg2, model, subs)
            vtype = GurobiRDDLCompiler._promote_vtype(vtype1, vtype2)
            vtype = GurobiRDDLCompiler._at_least_int(vtype)
            symb = symb1 or symb2
            
            if name == 'min': 
                if symb: 
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        min(lb1, lb2), min(ub1, ub2))
                    res = self._add_var(model, vtype, lb, ub)
                    model.addGenConstrMin(res, [gterm1, gterm2])
                else:
                    res = min(gterm1, gterm2)
                    lb, ub = res, res                    
                return res, vtype, lb, ub, symb
            
            elif name == 'max':
                if symb:
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        max(lb1, lb2), max(ub1, ub2))
                    res = self._add_var(model, vtype, lb, ub)
                    model.addGenConstrMax(res, [gterm1, gterm2])
                else:
                    res = max(gterm1, gterm2)
                    lb, ub = res, res                    
                return res, vtype, lb, ub, symb
            
            elif name == 'pow':
                if symb: 
                    # argument must be non-negative
                    lb1, ub1 = max(lb1, 0), max(ub1, 0)
                    base = self._add_var(model, vtype, lb1, ub1)
                    model.addGenConstrMax(base, [gterm1], constant=0)
                    
                    # compute bounds on pow
                    loglb = -math.inf if lb1 == 0 else math.log(lb1)
                    logub = -math.inf if ub1 == 0 else math.log(ub1)
                    loglu = (loglb * lb2, loglb * ub2, logub * lb2, logub * ub2)
                    logl, logu = min(loglu), max(loglu)
                    lb, ub = GurobiRDDLCompiler._fix_bounds(
                        math.exp(logl), math.exp(logu))
                    
                    # assign pow to new variable
                    res = self._add_real_var(model, lb, ub)
                    model.addGenConstrPow(
                        base, res, gterm2, options=self.pw_options)
                else:
                    res = math.pow(gterm1, gterm2)   
                    lb, ub = res, res                                
                return res, GRB.CONTINUOUS, lb, ub, symb
        
        raise RDDLNotImplementedError(
            f'Function operator {name} with {n} arguments is not '
            f'supported in Gurobi compiler.\n' + 
            print_stack_trace(expr))

    # ===========================================================================
    # control flow
    # ===========================================================================
    
    def _gurobi_control(self, expr, model, subs):
        _, op = expr.etype
        args = expr.args
        n = len(args)
        
        if n == 3 and op == 'if':
            pred, arg1, arg2 = args
            gpred, *_, symbp = self._gurobi(pred, model, subs)
            gterm1, vtype1, lb1, ub1, symb1 = self._gurobi(arg1, model, subs)
            gterm2, vtype2, lb2, ub2, symb2 = self._gurobi(arg2, model, subs)
            vtype = GurobiRDDLCompiler._promote_vtype(vtype1, vtype2)
            
            # compute bounds on if
            lb, ub = GurobiRDDLCompiler._fix_bounds(min(lb1, lb2), max(ub1, ub2))
            
            # assign if to new variable
            if symbp:
                res = self._add_var(model, vtype, lb, ub)
                model.addConstr((gpred == 1) >> (res == gterm1))
                model.addConstr((gpred == 0) >> (res == gterm2))
                symb = True
            elif gpred:
                res, symb = gterm1, symb1
            else:
                res, symb = gterm2, symb2
            return res, vtype, lb, ub, symb
            
        raise RDDLNotImplementedError(
            f'Control flow {op} with {n} arguments is not '
            f'supported in Gurobi compiler.\n' + 
            print_stack_trace(expr))

    # ===========================================================================
    # random variables
    # ===========================================================================
    
    def _gurobi_random(self, expr, model, subs):
        _, name = expr.etype
        if name == 'KronDelta':
            return self._gurobi_kron(expr, model, subs)
        elif name == 'DiracDelta':
            return self._gurobi_dirac(expr, model, subs)
        elif name == 'Uniform':
            return self._gurobi_uniform(expr, model, subs)
        elif name == 'Bernoulli':
            return self._gurobi_bernoulli(expr, model, subs)
        elif name == 'Normal':
            return self._gurobi_normal(expr, model, subs)
        elif name == 'Poisson':
            return self._gurobi_poisson(expr, model, subs)
        elif name == 'Exponential':
            return self._gurobi_exponential(expr, model, subs)
        elif name == 'Gamma':
            return self._gurobi_gamma(expr, model, subs)
        else:
            raise RDDLNotImplementedError(
                f'Distribution {name} is not supported in Gurobi compiler.\n' + 
                print_stack_trace(expr))
    
    def _gurobi_kron(self, expr, model, subs): 
        arg, = expr.args
        return self._gurobi(arg, model, subs)
    
    def _gurobi_dirac(self, expr, model, subs):
        arg, = expr.args
        return self._gurobi(arg, model, subs)
    
    def _gurobi_uniform(self, expr, model, subs):
        arg1, arg2 = expr.args
        gterm1, _, lb1, ub1, symb1 = self._gurobi(arg1, model, subs)
        gterm2, _, lb2, ub2, symb2 = self._gurobi(arg2, model, subs)
        
        # determinize uniform as (lower + upper) / 2        
        symb = symb1 or symb2
        midpoint = (gterm1 + gterm2) / 2
        if symb:
            lb, ub = GurobiRDDLCompiler._fix_bounds(
                (lb1 + lb2) / 2, (ub1 + ub2) / 2)
            res = self._add_real_var(model, lb, ub)
            model.addConstr(res == midpoint)            
        else:
            res = midpoint
            lb, ub = res, res
        return res, GRB.CONTINUOUS, lb, ub, symb
        
    def _gurobi_bernoulli(self, expr, model, subs):
        arg, = expr.args
        gterm, _, lb, ub, symb = self._gurobi(arg, model, subs)
        
        # determinize bernoulli as indicator of p > 0.5
        if symb:
            res = self._add_bool_var(model)
            model.addConstr((res == 1) >> (gterm >= 0.5 + self.epsilon))
            model.addConstr((res == 0) >> (gterm <= 0.5))
        else:
            res = gterm > 0.5
            lb, ub = res, res
        return res, GRB.BINARY, lb, ub, symb
        
    def _gurobi_normal(self, expr, model, subs):
        mean, _ = expr.args
        gterm, _, lb, ub, symb = self._gurobi(mean, model, subs)
        
        # determinize Normal as mean
        return gterm, GRB.CONTINUOUS, lb, ub, symb
    
    def _gurobi_poisson(self, expr, model, subs):
        rate, = expr.args
        gterm, _, lb, ub, symb = self._gurobi(rate, model, subs)
        
        # determinize Poisson as rate
        return gterm, GRB.CONTINUOUS, lb, ub, symb
    
    def _gurobi_exponential(self, expr, model, subs):
        scale, = expr.args
        gterm, _, lb, ub, symb = self._gurobi(scale, model, subs)
        
        # determinize Exponential as scale
        return gterm, GRB.CONTINUOUS, lb, ub, symb

    def _gurobi_gamma(self, expr, model, subs):
        shape, scale = expr.args
        gterm1, _, lb1, ub1, symb1 = self._gurobi(shape, model, subs)
        gterm2, _, lb2, ub2, symb2 = self._gurobi(scale, model, subs)
        
        # determinize gamma as shape * scale
        symb = symb1 or symb2
        if symb:
            cross_lb_ub = (lb1 * lb2, lb1 * ub2, ub1 * lb2, ub1 * ub2)
            lb, ub = GurobiRDDLCompiler._fix_bounds(
                min(cross_lb_ub), max(cross_lb_ub))
            res = self._add_real_var(model, lb, ub)
            model.addConstr(res == gterm1 * gterm2)
        else:
            res = gterm1 * gterm2
            lb, ub = res, res                    
        return res, GRB.CONTINUOUS, lb, ub, symb
        
