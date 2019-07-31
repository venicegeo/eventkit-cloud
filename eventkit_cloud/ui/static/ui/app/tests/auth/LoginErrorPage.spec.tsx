import * as React from 'react';
import * as sinon from 'sinon';
import { shallow } from 'enzyme';
import Paper from '@material-ui/core/Paper';
import * as utils from '../../utils/generic';
import { LoginErrorPage } from '../../components/auth/LoginErrorPage';
import { ErrorMessage } from '../../components/auth/ErrorMessage';


describe('LoginErrorPage component', () => {

    function getWrapper() {
        return shallow(<LoginErrorPage {...(global as any).eventkit_test_props} />);
    }

    it('should render just the login error paper', () => {
        const isValidStub = sinon.stub(utils, 'isBrowserValid')
            .returns(true);
        const wrapper = getWrapper();
        expect(wrapper.find(Paper)).toHaveLength(1);
        expect(wrapper.find(ErrorMessage)).toHaveLength(1);
        expect(wrapper.find('.qa-LoginErrorPage-browser-text')).toHaveLength(1);
        isValidStub.restore();
    });

});
